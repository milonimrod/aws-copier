---
phase: 01-core-reliability
reviewed: 2026-04-25T00:00:00Z
depth: standard
files_reviewed: 11
files_reviewed_list:
  - aws_copier/core/file_listener.py
  - aws_copier/core/folder_watcher.py
  - aws_copier/core/ignore_rules.py
  - aws_copier/models/simple_config.py
  - main.py
  - pyproject.toml
  - tests/unit/test_file_listener.py
  - tests/unit/test_folder_watcher.py
  - tests/unit/test_ignore_rules.py
  - tests/unit/test_signal_handling.py
  - tests/unit/test_simple_config.py
findings:
  critical: 1
  warning: 4
  info: 4
  total: 9
status: issues_found
---

# Phase 01: Code Review Report

**Reviewed:** 2026-04-25
**Depth:** standard
**Files Reviewed:** 11
**Status:** issues_found

## Summary

This phase delivers the core reliability improvements: centralized ignore rules, async-safe backup info I/O, concurrent uploads with correct semaphore wiring, graceful shutdown with upload drain, and cross-platform signal handling. The overall design is solid and the tests are thorough. One critical security issue exists — AWS credentials are serialized to disk in plaintext — and four correctness/reliability warnings need attention before this code goes to production.

---

## Critical Issues

### CR-01: AWS credentials written to disk in plaintext

**File:** `aws_copier/models/simple_config.py:55-70`
**Issue:** `save_to_yaml` unconditionally writes `aws_access_key_id` and `aws_secret_access_key` as plaintext YAML fields. `load_config` calls `save_to_yaml` at line 106 to create a template on first run (`main.py:178` also calls `save_to_yaml`). Any real credentials typed into the config file are silently persisted to disk. Additionally, `to_dict` (lines 83-93) returns the raw secret in its output, meaning any caller that logs or serializes `to_dict()` leaks credentials.

**Fix:** Exclude credentials from disk serialization. Use environment variables or a credential file separate from the app config:

```python
def save_to_yaml(self, config_path: Path) -> None:
    """Save configuration to YAML file — credentials are intentionally omitted."""
    data = {
        # NOTE: aws_access_key_id / aws_secret_access_key are NOT saved;
        # set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars instead.
        "aws_region": self.aws_region,
        "s3_bucket": self.s3_bucket,
        "s3_prefix": self.s3_prefix,
        "watch_folders": {str(k): v for k, v in self.folder_s3_mapping.items()},
        "max_concurrent_uploads": self.max_concurrent_uploads,
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, indent=2)
```

Similarly redact credentials from `to_dict` or add a separate `to_safe_dict` for logging.

---

## Warnings

### WR-01: `observer.join()` blocks the asyncio event loop

**File:** `aws_copier/core/folder_watcher.py:160`
**Issue:** `FolderWatcher.stop()` is `async`, but calls `self.observer.join(timeout=5)` synchronously. The watchdog `Observer.join` is a blocking `threading.Thread.join`, which will block the entire asyncio event loop for up to 5 seconds. Under load, this can delay or prevent the upload-drain step in `main.shutdown()`.

**Fix:** Offload the blocking call to a thread pool:

```python
async def stop(self) -> None:
    if not self.running:
        return
    logger.info("Stopping folder watcher")
    self.observer.stop()
    await asyncio.get_running_loop().run_in_executor(None, self.observer.join, 5)
    self.running = False
    self.handlers.clear()
    logger.info("Folder watcher stopped")
```

### WR-02: `updated_backup_info` retains stale entries for deleted files

**File:** `aws_copier/core/file_listener.py:146-156`
**Issue:** In `_process_current_folder`, `updated_backup_info` starts as a copy of `existing_backup_info` (line 146) and files from `current_files` are merged in, but entries for files that no longer exist on disk are never removed. Over time, deleted local files accumulate as phantom entries in `.milo_backup.info`. On the next scan their MD5 will be absent from `current_files`, so `_determine_files_to_upload` will not include them — correct — but the stale key persists in the info file indefinitely and will confuse any future tooling or manual inspection.

**Fix:** Replace the copy-then-patch pattern with a rebuild from `current_files` only:

```python
# Replace lines 146-156 with:
updated_backup_info = {}
for filename in uploaded_files:
    if filename in current_files:
        updated_backup_info[filename] = current_files[filename]
# Keep unchanged files that were NOT in the upload queue
for filename, md5_hash in current_files.items():
    if filename not in files_to_upload:
        updated_backup_info[filename] = md5_hash
```

This ensures `updated_backup_info` only contains files that currently exist on disk.

### WR-03: No validation on `max_concurrent_uploads`; value 0 creates a deadlock

**File:** `aws_copier/models/simple_config.py:42`
**Issue:** `max_concurrent_uploads` accepts any integer from the YAML with no lower-bound check. A value of `0` is silently passed to `asyncio.Semaphore(0)` in `FileListener.__init__` (line 35), which creates a semaphore that never releases permits — every upload will hang indefinitely at `async with self.upload_semaphore`.

**Fix:**

```python
raw = kwargs.get("max_concurrent_uploads", 100)
self.max_concurrent_uploads: int = max(1, int(raw))
```

### WR-04: `_process_current_folder` is not protected by the folder lock during the full read-modify-write cycle

**File:** `aws_copier/core/file_listener.py:123-168`
**Issue:** The per-folder `asyncio.Lock` (`_get_folder_lock`) is acquired inside `_load_backup_info` (around the file read) and again inside `_update_backup_info` (around the file write), but the full read-MD5-compare-upload-write sequence in `_process_current_folder` is not enclosed in a single lock acquisition. A real-time watchdog event can call `_process_current_folder` for the same folder concurrently with the ongoing full scan, causing two coroutines to race through the read-modify-write cycle and silently overwrite each other's updates.

**Fix:** Acquire the folder lock for the entire `_process_current_folder` body:

```python
async def _process_current_folder(self, folder_path: Path) -> None:
    async with self._get_folder_lock(folder_path):
        backup_info_file = folder_path / self.backup_info_filename
        # load without inner lock (already holding folder lock)
        existing_backup_info = await self._load_backup_info_unlocked(backup_info_file)
        ...
```

Alternatively, make `_load_backup_info` and `_update_backup_info` not acquire the lock themselves when the caller already holds it (e.g., an internal `_locked=False` variant).

---

## Info

### IN-01: Duplicate and conflicting dependency declarations in `pyproject.toml`

**File:** `pyproject.toml:14-24` and `pyproject.toml:84-92`
**Issue:** Dev dependencies are declared in two separate places: `[project.optional-dependencies] dev` (lines 14-24) and `[dependency-groups] dev` (lines 84-92). The two groups have overlapping but not identical packages (`pytest-asyncio>=0.21.0` vs `>=1.1.0`, `ruff>=0.1.0` vs `>=0.12.11`). The `[dependency-groups]` section is the uv-native format and takes precedence in `uv sync`, meaning the `[project.optional-dependencies]` section is dead configuration that will never be installed. `python-dotenv` and `moto[s3]` appear only in `[dependency-groups]`, so they will be missing for anyone using `pip install -e .[dev]`.

**Fix:** Remove the `[project.optional-dependencies]` section entirely and keep only `[dependency-groups] dev`. If pip compatibility is needed, consolidate all dev deps there with consistent version constraints.

### IN-02: Emoji characters in log messages

**File:** `main.py:39,48,52,55,60,90,109,111` and `aws_copier/core/folder_watcher.py:99,104`
**Issue:** Log messages use emoji (`✅`, `🚀`, `🛑`, `📊`, `📁`). This can corrupt log output in terminals or log aggregation systems without full Unicode support (e.g., Windows console with default CP1252 encoding, some syslog sinks). The project's own `CLAUDE.md` convention says "only use emojis if explicitly requested."

**Fix:** Replace emoji with plain-text equivalents, e.g. `"S3 Manager initialized"` instead of `"✅ S3 Manager initialized"`.

### IN-03: Stale entries are never cleaned from `_folder_locks` registry

**File:** `aws_copier/core/file_listener.py:61-72`
**Issue:** `_get_folder_lock` grows the `_folder_locks` dict indefinitely as new directories are discovered during recursive scans. For very large or frequently changing directory trees this is a minor memory leak, but more importantly the dict is never pruned even after a watched folder is removed from the config.

**Fix:** This is low-priority for a daemon that restarts periodically, but a `WeakValueDictionary` or periodic pruning on `reset_statistics` would prevent unbounded growth.

### IN-04: Test file imports `hashlib` inside test methods

**File:** `tests/unit/test_file_listener.py:108,206`
**Issue:** `import hashlib` is done inline inside test methods rather than at the top of the module. This is a minor style inconsistency with no functional impact, but it contradicts the project's import organization convention.

**Fix:** Move `import hashlib` to the top-level imports block in `test_file_listener.py`.

---

_Reviewed: 2026-04-25_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
