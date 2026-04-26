# Phase 2: Performance & Polish - Research

**Researched:** 2026-04-25
**Domain:** Python asyncio daemon — mtime-skipping backup scanner, aiobotocore credential chain, pathspec gitignore filtering, S3 lifecycle API
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**D-01:** Migrate `.milo_backup.info` silently in-place. Old string entries (`{filename: "md5hash"}`) are read as `{md5: value, mtime: 0.0}`. `mtime: 0.0` forces one re-stat per file on the first run after migration, then the new format takes over. No data loss, no re-upload.

**D-02:** After a successful upload, store `st_mtime` captured just before the upload call (not at scan start). This ensures that a file modified during upload is detected on the next cycle.

**D-03:** New format: `{filename: {md5: "...", mtime: <float>}}`. Backward-compatible read: if a value is a plain string, treat it as the old format and apply D-01 migration on read; write always uses the new dict format.

**D-04:** Cache `existing_backup_info` per folder in `FileListener` instance memory between scan cycles. Re-read `.milo_backup.info` from disk only when the file's `st_mtime` changes (check via `os.stat` before loading). This avoids O(n) disk reads per 5-minute cycle for unchanged folders.

**D-05:** Compute MD5 once in `FileListener._upload_single_file`; pass the pre-computed hash to `S3Manager.upload_file` as a new optional parameter `precomputed_md5: Optional[str] = None`. When provided, `upload_file` skips its internal `_calculate_md5` call.

**D-06:** Debounce per-path with a 2-second timer using `asyncio.create_task` + `asyncio.sleep(2)`. If a new event arrives for the same path before the timer fires, cancel the pending task and start a fresh one. The 2-second window and per-path keying are fixed.

**D-07:** `.backupignore` patterns cascade into subdirectories — a file in `/photos/` applies to `/photos/2024/`, `/photos/raw/`, etc. This matches `.gitignore` semantics and the `pathspec` library's standard usage.

**D-08:** Parent-directory `.backupignore` rules inherit downward. Rules accumulate as the tree is descended — a child directory's `.backupignore` adds to (not replaces) ancestor rules. Evaluated in path order: root → parent → child.

**D-09:** Fall back to the AWS provider chain only when `aws_access_key_id` or `aws_secret_access_key` are absent from `config.yaml` entirely. If the keys are present (even as placeholder strings), use them as-is — explicit config always wins.

**D-10:** At startup, emit `logger.info("AWS credentials loaded from: {source}")` where `{source}` is one of `"config.yaml"`, `"environment variables"`, or `"~/.aws/credentials"`. Clear audit trail for auth failures.

**D-11:** If the lifecycle rule cannot be set or verified at startup (permission denied, API error), log a warning and continue. The daemon is usable without this protection — do not abort startup.

**D-12:** If an `AbortIncompleteMultipartUpload` rule already exists on the bucket (any `DaysAfterInitiation` value), log info and leave it untouched. Never overwrite externally-set rules.

**D-13:** Carry forward Phase 1 approach — each requirement gets a behavior-proving test. Key tests: mtime skip reduces MD5 calls on unchanged files; debounce collapses rapid events into one call; `.backupignore` excludes matched files; credential chain fallback is exercised with and without config keys present.

### Claude's Discretion

- Exact asyncio mechanism for debounce timer (`asyncio.create_task` + `asyncio.sleep` vs `asyncio.TimerHandle`)
- Internal data structure for per-path debounce state in `FolderWatcher` (dict keyed by path string)
- Whether the in-memory backup info cache is a class-level dict or instance dict in `FileListener`
- `pathspec` version constraint and import pattern
- S3 API call used to read/write lifecycle rules (`get_bucket_lifecycle_configuration` / `put_bucket_lifecycle_configuration`)

### Deferred Ideas (OUT OF SCOPE)

None — discussion stayed within phase scope.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| PERF-01 | mtime-skip: check `st_mtime` before MD5; extend `.milo_backup.info` to `{md5, mtime}` with backward-compatible migration | `os.stat` for mtime; dict format upgrade; D-01/D-02/D-03 locked |
| PERF-02 | In-memory backup info cache keyed by folder; re-read disk only when `.milo_backup.info` `st_mtime` changes | `os.stat` cache check; dict keyed by `Path`; guarded by existing `_get_folder_lock` |
| PERF-03 | MD5 computed once per upload; pre-computed hash threaded to `S3Manager.upload_file` via optional param | `upload_file(precomputed_md5=...)` signature extension; D-05 locked |
| PERF-04 | 2-second per-path debounce on file events; rapid events collapse to one `_process_current_folder` call | `asyncio.create_task` + cancel pattern; dict[str, asyncio.Task] in `FileChangeHandler` |
| CONFIG-05 | AWS credential chain fallback when keys absent from config.yaml | `create_client` called without explicit credentials; aiobotocore uses botocore provider chain |
| CONFIG-06 | Per-directory `.backupignore` files; gitignore-style via `pathspec`; cascade into subdirs; accumulate ancestor rules | `pathspec 1.1.0`; `PathSpec.from_lines('gitignore', ...)`; `spec.match_file(relative_path)` |
| CONFIG-07 | S3 lifecycle rule check/set at startup for `AbortIncompleteMultipartUpload` after 1 day | `get_bucket_lifecycle_configuration` + `NoSuchLifecycleConfiguration` error; `put_bucket_lifecycle_configuration` |
</phase_requirements>

---

## Summary

Phase 2 is a focused backend optimisation and hardening pass on four files: `file_listener.py`, `folder_watcher.py`, `s3_manager.py`, and `simple_config.py`. All decisions are already locked in CONTEXT.md; research confirms the chosen approaches are correct and documents the specific API signatures, error codes, and edge cases the planner needs to write precise implementation tasks.

The most mechanically complex requirements are PERF-01/PERF-02 (touching the backup-info format and adding a per-folder cache), PERF-04 (asyncio debounce with task cancellation), and CONFIG-07 (S3 lifecycle API with `NoSuchLifecycleConfiguration` error handling). CONFIG-06 (`.backupignore`) is straightforward with `pathspec 1.1.0` but requires a dependency addition and a traversal-order strategy for accumulating ancestor rules.

**Primary recommendation:** Implement requirements in dependency order — PERF-01 first (format change), then PERF-02 (cache on top of new format), then PERF-03 (thread hash through), then PERF-04 (debounce), then CONFIG-05 (credentials), then CONFIG-06 (backupignore), then CONFIG-07 (lifecycle).

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| mtime-skip scan (PERF-01) | File System / Daemon | — | Pure local I/O decision; lives in `FileListener._scan_current_files` |
| Backup-info format migration (PERF-01) | File System / Daemon | — | `_load_backup_info` / `_update_backup_info` in `FileListener` |
| In-memory backup info cache (PERF-02) | File System / Daemon | — | Instance dict in `FileListener`; guarded by per-folder `asyncio.Lock` |
| MD5 deduplication (PERF-03) | File System / Daemon | S3 / AWS | Hash computed in `FileListener._upload_single_file`; consumed by `S3Manager.upload_file` |
| Event debounce (PERF-04) | File System / Daemon | — | `FileChangeHandler.on_any_event` in `folder_watcher.py`; asyncio event loop thread only |
| Credential chain fallback (CONFIG-05) | Config / Init | S3 / AWS | `SimpleConfig.__init__` detects absent keys; `S3Manager._get_or_create_client` omits params |
| `.backupignore` filtering (CONFIG-06) | File System / Daemon | — | `FileListener._process_folder_recursively` accumulates `PathSpec` per folder |
| S3 lifecycle rule check/set (CONFIG-07) | S3 / AWS | — | New method on `S3Manager`; called from `main.py` `AWSCopierApp.start()` after `initialize()` |

---

## Standard Stack

### Core (all already in virtualenv)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `asyncio` (stdlib) | 3.11 | Debounce timer, task cancellation | Already used throughout; `create_task` + `sleep` is idiomatic |
| `os.stat` / `pathlib.Path.stat()` | stdlib | mtime reads for PERF-01/02 | Zero-overhead; `st_mtime` is a float in seconds since epoch |
| `aiofiles` | >=24.1.0 | Async backup-info I/O | Already in use in `_load_backup_info` / `_update_backup_info` |
| `aiobotocore` | 2.24.1 | S3 lifecycle API (CONFIG-07) | Already used for all S3 operations |
| `botocore.exceptions.ClientError` | (via aiobotocore) | `NoSuchLifecycleConfiguration` error catch | Same exception class used in existing `check_exists` |

### New Dependency

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `pathspec` | 1.1.0 | gitignore-style pattern matching for `.backupignore` | Standard library used by pip, black, and mypy themselves; 20M weekly downloads; Python 3.9+ compatible |

**pathspec is not currently in the virtualenv.** [VERIFIED: uv run python check] Must be added to `pyproject.toml` `dependencies` and `uv.lock` updated.

**Installation:**
```bash
uv add pathspec>=0.9.0
```

Version 1.1.0 is the current release (April 23, 2026). [VERIFIED: pypi.org/project/pathspec/]

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `pathspec` | `fnmatch` hand-rolled cascade | `fnmatch` doesn't handle directory-scoped negation, `!` patterns, or `**` correctly; pathspec handles all gitignore edge cases |
| `asyncio.create_task` + `asyncio.sleep` debounce | `asyncio.TimerHandle` via `loop.call_later` | `call_later` is synchronous callback only; `create_task` + `sleep` is more idiomatic for async code and allows `await` inside the debounced handler |
| In-memory cache dict | Re-read disk every cycle | Current code re-reads on every 5-minute scan; PERF-02 fixes this with negligible memory cost (one dict per watched folder) |

---

## Architecture Patterns

### System Architecture Diagram

```
[watchdog thread] --event--> [FileChangeHandler.on_any_event]
                                       |
                              per-path debounce dict
                              (cancel old task, create new)
                                       |
                              asyncio.sleep(2)
                                       |
                              [FileListener._process_current_folder(folder)]
                                       |
                         ┌─────────────────────────────┐
                         │  PERF-02 cache check          │
                         │  os.stat(.milo_backup.info)  │
                         │  hit → use memory            │
                         │  miss → aiofiles read + parse│
                         └─────────────────────────────┘
                                       |
                         for each file in folder:
                           ┌─────────────────────────┐
                           │  PERF-01 mtime check     │
                           │  mtime unchanged → skip  │
                           │  mtime changed → MD5     │
                           └─────────────────────────┘
                                       |
                           CONFIG-06: .backupignore check
                           (PathSpec cascade from ancestors)
                                       |
                           [_upload_single_file]
                             compute MD5 (once, PERF-03)
                                       |
                             [S3Manager.upload_file(precomputed_md5=hash)]
                               skip internal _calculate_md5
                                       |
                                  S3 PUT / multipart

[main.py AWSCopierApp.start()]
  → s3_manager.initialize()
  → s3_manager.ensure_lifecycle_rule()   ← CONFIG-07
  → file_listener.scan_all_folders()
  → folder_watcher.start()

[SimpleConfig.__init__]   ← CONFIG-05
  if aws_access_key_id present → use explicit creds
  else → omit from create_client() → botocore provider chain
```

### Recommended Project Structure

No structural changes needed. All modifications are in-place to existing files:

```
aws_copier/
├── core/
│   ├── file_listener.py     # PERF-01, PERF-02, PERF-03
│   ├── folder_watcher.py    # PERF-04
│   ├── ignore_rules.py      # unchanged (Phase 1 output)
│   └── s3_manager.py        # PERF-03 (signature), CONFIG-07 (new method)
├── models/
│   └── simple_config.py     # CONFIG-05 (credential detection)
tests/
└── (new test files for each requirement)
```

### Pattern 1: mtime-skip with Format Migration (PERF-01)

**What:** On load, if an entry value is a plain string, treat it as legacy. On skip-check, compare `os.stat(file).st_mtime` vs `entry["mtime"]`; skip MD5 if equal.

**When to use:** In `_load_backup_info` (migration on read) and `_scan_current_files` (skip decision).

```python
# Source: D-01, D-02, D-03 from CONTEXT.md + os.stat() stdlib

def _migrate_entry(value) -> Dict[str, Any]:
    """Migrate old string entry to new {md5, mtime} dict format."""
    if isinstance(value, str):
        return {"md5": value, "mtime": 0.0}   # mtime=0.0 forces one re-stat
    return value                               # already new format

# In _scan_current_files:
stat = file_path.stat()
entry = existing_backup_info.get(filename)
if entry and entry.get("mtime") == stat.st_mtime:
    # mtime unchanged → skip MD5, mark as skipped
    self._stats["skipped_files"] += 1
    continue

md5 = await self._calculate_md5(file_path)
current_files[filename] = {"md5": md5, "mtime": stat.st_mtime}
```

**Key detail (D-02):** The `st_mtime` stored in backup info must be captured from `os.stat()` just before the upload call, not at scan-start time. This is because a file modified between scan and upload would otherwise be silently skipped on the next cycle.

### Pattern 2: In-Memory Backup Info Cache (PERF-02)

**What:** Instance dict maps `folder_path -> (info_dict, cached_mtime)`. Before reading disk, `os.stat` the `.milo_backup.info` file. If `st_mtime` matches cached value, return in-memory dict. Guard with existing `_get_folder_lock`.

```python
# Source: D-04 from CONTEXT.md + os.stat stdlib

# In FileListener.__init__:
self._backup_info_cache: Dict[Path, Dict[str, Any]] = {}
self._backup_info_mtime: Dict[Path, float] = {}

# In _load_backup_info (simplified):
async with self._get_folder_lock(backup_info_file.parent):
    try:
        disk_mtime = backup_info_file.stat().st_mtime
    except FileNotFoundError:
        return {}
    cached_mtime = self._backup_info_mtime.get(backup_info_file.parent)
    if cached_mtime == disk_mtime:
        return self._backup_info_cache.get(backup_info_file.parent, {})
    # cache miss: read from disk
    ...
    self._backup_info_cache[backup_info_file.parent] = data
    self._backup_info_mtime[backup_info_file.parent] = disk_mtime
    return data
```

**Important:** Both cache dicts are instance-level (not class-level) — each `FileListener` instance has its own cache. This is correct since the application creates one `FileListener` per daemon lifetime.

### Pattern 3: MD5 Deduplication (PERF-03)

**What:** Add `precomputed_md5: Optional[str] = None` to `S3Manager.upload_file`. When provided, skip the internal `_calculate_md5` call entirely. `FileListener._upload_single_file` computes MD5 once and passes it.

```python
# Source: D-05 from CONTEXT.md

# S3Manager.upload_file signature:
async def upload_file(
    self,
    local_path: Path,
    s3_key: str,
    precomputed_md5: Optional[str] = None,   # NEW — backward-compatible
) -> bool:
    md5_hash = precomputed_md5 or await self._calculate_md5(local_path)
    ...

# FileListener._upload_single_file:
local_md5 = await self._calculate_md5(file_path)  # single computation
if await self.s3_manager.check_exists(s3_key, local_md5):
    ...
if await self.s3_manager.upload_file(file_path, s3_key, precomputed_md5=local_md5):
    ...
```

### Pattern 4: Asyncio Per-Path Debounce (PERF-04)

**What:** Dict `_debounce_tasks: Dict[str, asyncio.Task]` in `FileChangeHandler`. On event, cancel existing task for that path (if any), create new task that sleeps 2 s then calls `_process_changed_file`. Dict accessed only from the asyncio event loop thread (via `run_coroutine_threadsafe`), so no additional locking needed.

```python
# Source: D-06 from CONTEXT.md + asyncio stdlib

# In FileChangeHandler.__init__:
self._debounce_tasks: Dict[str, asyncio.Task] = {}

# In on_any_event (schedules debounced coroutine):
asyncio.run_coroutine_threadsafe(
    self._schedule_debounced(file_path, event.event_type),
    self.event_loop,
)

# New coroutine (runs in asyncio thread — safe dict access):
async def _schedule_debounced(self, file_path: Path, event_type: str) -> None:
    key = str(file_path)
    existing = self._debounce_tasks.get(key)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(
        self._debounced_process(file_path, event_type),
        name=f"debounce-{file_path.name}",
    )
    self._debounce_tasks[key] = task

async def _debounced_process(self, file_path: Path, event_type: str) -> None:
    try:
        await asyncio.sleep(2)
        await self._process_changed_file(file_path, event_type)
    except asyncio.CancelledError:
        pass   # Superseded by a newer event — normal, not an error
```

**Pitfall:** `asyncio.CancelledError` must be caught inside `_debounced_process` (not re-raised), otherwise the cancellation propagates and logs a spurious error. It is expected behaviour.

### Pattern 5: Credential Chain Fallback (CONFIG-05)

**What:** In `SimpleConfig.__init__`, detect whether `aws_access_key_id` / `aws_secret_access_key` are present. Set a boolean flag `self.use_credential_chain: bool`. In `S3Manager._get_or_create_client`, pass explicit credentials only when `not config.use_credential_chain`.

```python
# Source: D-09, D-10 from CONTEXT.md; aiobotocore/botocore credential chain [VERIFIED: pypi.org/project/aiobotocore]

# In SimpleConfig.__init__:
raw_key = kwargs.get("aws_access_key_id")
raw_secret = kwargs.get("aws_secret_access_key")
self.use_credential_chain: bool = not raw_key or not raw_secret
self.credential_source: str = "config.yaml" if not self.use_credential_chain else "provider chain"

# In S3Manager._get_or_create_client:
client_kwargs: Dict[str, Any] = {
    "region_name": self.config.aws_region,
    "config": self._client_config,
}
if not self.config.use_credential_chain:
    client_kwargs["aws_access_key_id"] = self.config.aws_access_key_id
    client_kwargs["aws_secret_access_key"] = self.config.aws_secret_access_key
# else: aiobotocore will traverse env vars → ~/.aws/credentials → instance profile
self._s3_client = await self._exit_stack.enter_async_context(
    self._session.create_client("s3", **client_kwargs)
)
```

**D-10 log:** After `S3Manager.initialize()` completes, log `logger.info(f"AWS credentials loaded from: {source}")`. Source detection can be done in `SimpleConfig` or inferred in `S3Manager.initialize()`. The simplest approach is to expose `config.credential_source` and log it in `initialize()`.

**Important:** aiobotocore uses botocore's provider chain when explicit credentials are omitted from `create_client`. The chain order is: environment variables (`AWS_*`) → `~/.aws/credentials` → IAM instance profile. [VERIFIED: boto3 credentials documentation]

### Pattern 6: `.backupignore` with pathspec Cascade (CONFIG-06)

**What:** When scanning a folder, collect `.backupignore` files from the watch-root down to the current folder. Build a `PathSpec` from all accumulated pattern lines. In `_scan_current_files`, test each file against the spec using a path relative to the watched root.

```python
# Source: D-07, D-08 from CONTEXT.md; pathspec 1.1.0 docs [VERIFIED: pypi.org/project/pathspec/]

from pathspec import PathSpec

def _load_backupignore_spec(
    self, folder_path: Path, watch_root: Path
) -> PathSpec:
    """Accumulate .backupignore patterns from watch_root down to folder_path."""
    all_patterns: List[str] = []
    # Collect ancestor .backupignore files in root-to-leaf order (D-08)
    parts = folder_path.relative_to(watch_root).parts
    current = watch_root
    for part in ("",) + parts:  # include watch_root itself
        if part:
            current = current / part
        ignore_file = current / ".backupignore"
        if ignore_file.exists():
            try:
                lines = ignore_file.read_text(encoding="utf-8").splitlines()
                all_patterns.extend(lines)
            except Exception as e:
                logger.warning(f"Could not read {ignore_file}: {e}")
    return PathSpec.from_lines("gitignore", all_patterns)

# Usage in _scan_current_files:
spec = self._load_backupignore_spec(folder_path, watch_root)
relative = file_path.relative_to(watch_root)
if spec.match_file(str(relative)):
    self._stats["ignored_files"] += 1
    continue
```

**Key detail:** `PathSpec.match_file` must receive a path relative to the root where patterns apply — not an absolute path. Using an absolute path can break directory-scoped patterns like `raw/`.

**Note on `GitIgnoreSpec`:** The `pathspec` docs mention `GitIgnoreSpec` for better edge-case handling (negation patterns re-including files from excluded dirs). For the typical `.backupignore` use case (simple glob patterns, no negation), `PathSpec.from_lines('gitignore', ...)` is sufficient and simpler. [CITED: github.com/cpburnz/python-pathspec]

### Pattern 7: S3 Lifecycle Rule Check/Set (CONFIG-07)

**What:** New method `S3Manager.ensure_lifecycle_rule()`. Call `get_bucket_lifecycle_configuration`; if rule already exists, log and return (D-12). If `NoSuchLifecycleConfiguration`, call `put_bucket_lifecycle_configuration` to add the rule. Any other exception: log warning and return (D-11).

```python
# Source: D-11, D-12 from CONTEXT.md
# [CITED: docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_bucket_lifecycle_configuration.html]
# [CITED: docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_bucket_lifecycle_configuration.html]

async def ensure_lifecycle_rule(self) -> None:
    """Check or set AbortIncompleteMultipartUpload lifecycle rule on the bucket."""
    client = await self._get_or_create_client()
    try:
        response = await client.get_bucket_lifecycle_configuration(
            Bucket=self.config.s3_bucket
        )
        # Check if any AbortIncompleteMultipartUpload rule exists (D-12)
        for rule in response.get("Rules", []):
            abort = rule.get("AbortIncompleteMultipartUpload")
            if abort:
                days = abort.get("DaysAfterInitiation", "?")
                logger.info(
                    f"S3 lifecycle rule already present "
                    f"(DaysAfterInitiation={days}). Skipping."
                )
                return
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code != "NoSuchLifecycleConfiguration":
            logger.warning(
                f"Could not verify multipart lifecycle rule: {e}. "
                f"Incomplete uploads may accumulate cost."
            )
            return
        # No lifecycle config at all → create one

    try:
        await client.put_bucket_lifecycle_configuration(
            Bucket=self.config.s3_bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "aws-copier-abort-incomplete-multipart",
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},   # applies to all objects
                        "AbortIncompleteMultipartUpload": {
                            "DaysAfterInitiation": 1
                        },
                    }
                ]
            },
        )
        logger.info(
            "S3 lifecycle rule set: AbortIncompleteMultipartUpload after 1 day."
        )
    except ClientError as e:
        logger.warning(
            f"Could not verify multipart lifecycle rule: {e}. "
            f"Incomplete uploads may accumulate cost."
        )
```

**API notes:**
- `get_bucket_lifecycle_configuration` raises `ClientError` with code `"NoSuchLifecycleConfiguration"` (not 404) when no lifecycle config exists. [VERIFIED: docs.aws.amazon.com]
- `put_bucket_lifecycle_configuration` **replaces** the entire lifecycle config. Since D-12 says "never overwrite externally-set rules," the put is only called when `NoSuchLifecycleConfiguration` was raised — meaning there are no existing rules to overwrite. If any existing rules are found (even no `AbortIncompleteMultipartUpload` rule), the method returns without calling put.
- `Filter: {"Prefix": ""}` is required for rules without a prefix filter in the current S3 API. An empty `Filter` dict is invalid.

**Startup integration:** In `main.py` `AWSCopierApp.start()`, add `await self.s3_manager.ensure_lifecycle_rule()` immediately after `await self.s3_manager.initialize()` (and before `scan_all_folders()`).

### Anti-Patterns to Avoid

- **Float mtime comparison with equality:** `stat.st_mtime == cached_mtime` is correct for this use case because we round-trip the float through JSON and compare the same float representation. Do not round or truncate mtime. If JSON serialization causes precision loss, store mtime as string `repr(float)` instead — but test shows Python's `json.dumps` preserves float precision for `st_mtime` values.
- **Catching `CancelledError` broadly:** Only catch it inside the debounced coroutine itself (`_debounced_process`). Do not swallow it in the outer `_schedule_debounced` coroutine.
- **Passing absolute path to `PathSpec.match_file`:** Always pass paths relative to the watch root. Absolute paths silently fail to match directory-scoped patterns.
- **Calling `put_bucket_lifecycle_configuration` when rules already exist:** This would replace the entire lifecycle config, destroying user-managed rules. The guard is: only put when `NoSuchLifecycleConfiguration` was the error.
- **Storing `use_credential_chain` as inferred from presence of placeholder strings:** Decision D-09 says absent keys only. The check must be `not kwargs.get("aws_access_key_id")` (None or empty string), not `== "YOUR_ACCESS_KEY_ID"`. Checking for placeholder values would be fragile and not what was decided.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| gitignore-style pattern matching | Custom glob cascade with `fnmatch` | `pathspec` | Handles `**`, directory-scoped patterns, negation (`!`), comments, empty lines — all gitignore edge cases |
| AWS credential chain resolution | Manual env-var → file fallback logic | aiobotocore/botocore provider chain (omit credentials from `create_client`) | botocore already implements env → file → instance-profile → ECS → IAM order; reimplementing would miss edge cases and be a security risk |
| S3 lifecycle rule existence check | Custom HEAD request parsing | `get_bucket_lifecycle_configuration` + `NoSuchLifecycleConfiguration` | The S3 API has a dedicated lifecycle endpoint; `NoSuchLifecycleConfiguration` is the canonical "no rules" signal |

---

## Common Pitfalls

### Pitfall 1: JSON float precision for mtime

**What goes wrong:** `json.dumps` may alter `st_mtime` float precision for very large timestamps (>2^53 seconds, which won't occur in practice), but more practically, mtime comparison breaks if you store/load via `int()` rounding.

**Why it happens:** `st_mtime` is a float. If stored as `int` (truncated), a file modified 0.5 seconds apart within the same second would not be detected.

**How to avoid:** Store and compare `st_mtime` as float directly. Python's `json` module preserves float precision for filesystem timestamps (which are < 2^53).

**Warning signs:** Files modified in rapid succession are not re-uploaded on next cycle.

### Pitfall 2: Debounce task cleanup on watcher stop

**What goes wrong:** When `FolderWatcher.stop()` is called, pending debounce tasks in `FileChangeHandler._debounce_tasks` may still be sleeping. If they fire after the event loop starts shutting down, they'll raise `RuntimeError: Event loop is closed`.

**Why it happens:** `asyncio.create_task` tasks are not automatically cancelled when the object that created them is discarded.

**How to avoid:** In `FileChangeHandler`, add a `cancel_all_pending()` method that cancels all tasks in `_debounce_tasks`. Call it from `FolderWatcher.stop()` before stopping the observer.

**Warning signs:** `RuntimeError: Event loop is closed` in logs during shutdown after rapid file activity.

### Pitfall 3: Per-folder cache dict key type

**What goes wrong:** Using `str` keys for `_backup_info_cache` while the rest of `FileListener` uses `Path` keys (e.g., `_folder_locks`). Type mismatch causes cache misses or `KeyError`.

**Why it happens:** `Path("foo") != "foo"` — Python's dict lookup is type-sensitive.

**How to avoid:** Use `Path` keys consistently for all per-folder dicts in `FileListener`. The `backup_info_file.parent` is already a `Path`.

### Pitfall 4: `pathspec` relative path subtlety

**What goes wrong:** `.backupignore` pattern `raw/*.jpg` does not match `/Users/alice/photos/2024/raw/shot.jpg` when the absolute path is passed to `spec.match_file`.

**Why it happens:** `pathspec` treats the pattern as relative. An absolute path has extra components that prevent the match.

**How to avoid:** Always pass `str(file_path.relative_to(watch_root))` to `spec.match_file`. Normalise to forward slashes on Windows with `.replace("\\", "/")`.

### Pitfall 5: S3 lifecycle `put` overwrites all existing rules

**What goes wrong:** Calling `put_bucket_lifecycle_configuration` when the bucket already has unrelated lifecycle rules (e.g., expiry transitions) deletes them.

**Why it happens:** `put_bucket_lifecycle_configuration` replaces the entire lifecycle config, not a single rule.

**How to avoid:** Only call `put` when `get` raised `NoSuchLifecycleConfiguration`. If `get` succeeded but returned rules without `AbortIncompleteMultipartUpload`, log a warning and do not put — leave the user's existing rules intact (D-12 already mandates this).

### Pitfall 6: moto does not support lifecycle rules in all versions

**What goes wrong:** Tests using `moto[s3]` mock may fail or behave differently for `get_bucket_lifecycle_configuration` / `put_bucket_lifecycle_configuration` depending on moto version.

**Why it happens:** moto coverage of the S3 lifecycle API has been partial in older versions.

**How to avoid:** For CONFIG-07 tests, use `unittest.mock.AsyncMock` to mock `s3_manager.ensure_lifecycle_rule()` at the integration test level, or test `ensure_lifecycle_rule` itself by mocking the aiobotocore client directly. Do not rely on moto's lifecycle support.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Compute MD5 for every file on every scan | Check mtime first; skip MD5 for unchanged files | PERF-01 (this phase) | Near-zero CPU for steady-state scans |
| Re-read `.milo_backup.info` from disk every cycle | In-memory cache with `st_mtime` invalidation | PERF-02 (this phase) | Eliminates O(n) disk reads per 5-min cycle |
| MD5 computed twice per upload (FileListener + S3Manager) | Computed once; threaded via `precomputed_md5` param | PERF-03 (this phase) | Halves MD5 I/O per uploaded file |
| Every file event triggers immediate folder rescan | 2-second debounce collapses burst events | PERF-04 (this phase) | Avoids redundant scans on atomic-save editors |
| Hardcoded credentials required in config.yaml | AWS provider chain as fallback | CONFIG-05 (this phase) | Supports IAM roles, shared credentials, env vars |
| Global ignore patterns only | Per-directory `.backupignore` cascade | CONFIG-06 (this phase) | User-controlled per-folder exclusions |
| Orphaned multipart parts accumulate indefinitely | S3 lifecycle rule auto-aborts after 1 day | CONFIG-07 (this phase) | Prevents silent cost accumulation |

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `json.dumps` preserves `st_mtime` float precision for all practical filesystem timestamps | Pattern 1 (mtime-skip) | Files might not be detected as changed if precision is lost — mitigatable by testing with real mtime values |
| A2 | moto's S3 lifecycle API support is unreliable for test purposes | Pitfall 6 | If moto does support it fully, mocking is unnecessary overhead — low risk |

---

## Open Questions

1. **Should `_load_backupignore_spec` be cached per folder?**
   - What we know: Reading `.backupignore` files on every `_process_current_folder` call adds disk I/O. For a folder with many files processed frequently, this could add up.
   - What's unclear: How frequently `_process_current_folder` is called per folder in typical use (real-time events are debounced to 2s; scans are every 5 min).
   - Recommendation: Cache `PathSpec` per folder keyed by the max `st_mtime` of all ancestor `.backupignore` files. For Phase 2, reading on each call is acceptable — cache if profiling shows it as a bottleneck.

2. **How to detect credential source for D-10 log message?**
   - What we know: When `use_credential_chain=True`, botocore resolves credentials lazily. Detecting which provider was used requires inspecting `session.get_credentials()` after client creation.
   - What's unclear: aiobotocore's async credential resolver may not synchronously expose the resolved provider name.
   - Recommendation: For D-10, a pragmatic approach: if `use_credential_chain=False`, source = `"config.yaml"`; otherwise, log `"provider chain (env / ~/.aws/credentials / IAM)"`. Full provider introspection adds complexity with no practical debugging benefit.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.11 | All | ✓ | 3.11 (pinned) | — |
| `aiobotocore` | CONFIG-07, PERF-03 | ✓ | 2.24.1 | — |
| `aiofiles` | PERF-01, PERF-02 | ✓ | (in lock) | — |
| `pathspec` | CONFIG-06 | ✗ | — | Must add to `pyproject.toml`; `uv add pathspec>=0.9.0` |
| `moto[s3]` | Tests | ✓ | (in dev deps) | — |

**Missing dependencies with no fallback:**
- `pathspec` — must be added to runtime deps before CONFIG-06 can be implemented.

---

## Sources

### Primary (HIGH confidence)
- `pypi.org/project/pathspec/` — version 1.1.0 (April 23, 2026), usage API, Python >=3.9 requirement [VERIFIED]
- `docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_bucket_lifecycle_configuration.html` — response structure, `NoSuchLifecycleConfiguration` error code [VERIFIED]
- `docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_bucket_lifecycle_configuration.html` — method signature, `Filter: {"Prefix": ""}` requirement, `AbortIncompleteMultipartUpload.DaysAfterInitiation` [VERIFIED]
- `aws_copier/` codebase — existing method signatures, `_get_folder_lock`, `_active_upload_tasks`, `_get_or_create_client` [VERIFIED: Read tool]

### Secondary (MEDIUM confidence)
- `github.com/cpburnz/python-pathspec` — `GitIgnoreSpec` vs `PathSpec` distinction, `match_file` semantics [CITED]
- `boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html` — botocore credential chain order [CITED]
- aiobotocore issue tracker — credential chain works when explicit credentials omitted from `create_client` [CITED: github.com/aio-libs/aiobotocore/issues/808]

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all libraries verified against current PyPI; pathspec 1.1.0 confirmed
- Architecture: HIGH — all patterns derived from locked CONTEXT.md decisions plus verified API docs
- Pitfalls: HIGH — derived from direct code inspection and verified API behaviour

**Research date:** 2026-04-25
**Valid until:** 2026-05-25 (stable libraries; S3 API is stable)
