# Domain Pitfalls

**Domain:** Async Python S3 backup daemon
**Researched:** 2026-04-24
**Scope:** Fixing and improving async Python tools that interact with S3

---

## Critical Pitfalls

### Pitfall 1: `call_soon_threadsafe(asyncio.create_task, coro)` — wrong-loop task scheduling

**What goes wrong:**
`asyncio.create_task()` calls `asyncio.get_running_loop()` internally to determine which loop to schedule the task on. When `create_task` is passed as the callback to `call_soon_threadsafe`, it executes in the event loop thread — but at that moment `asyncio.get_running_loop()` returns the loop currently executing the callback, which is `self.event_loop`. The code in the codebase actually has the opposite problem: `call_soon_threadsafe` is called from the watchdog thread, where there is no running loop at all. Python 3.10+ changed `asyncio.get_running_loop()` to raise `RuntimeError` (not just return `None`) if no loop is running in the calling thread. So the call silently succeeds on Python 3.9 but raises `RuntimeError: no running event loop` on 3.10+.

The pattern also has a subtler failure mode: if `asyncio.create_task` is called before the loop is actually running (e.g., during startup), the task is created but never starts, with no error.

**Why it happens:**
`call_soon_threadsafe` is designed for scheduling plain callables, not coroutine-scheduling functions. Coroutine scheduling requires an already-running loop to attach the task to. The correct API for scheduling a coroutine from a non-loop thread is `asyncio.run_coroutine_threadsafe(coro, loop)`, which explicitly targets a specific loop and returns a `concurrent.futures.Future`.

**Warning signs:**
- `RuntimeError: no running event loop` in watchdog thread logs
- File change events logged by watchdog but no upload triggered
- Silent no-op on Python 3.9 when no loop is yet running
- Upload tasks that appear to be created but never execute

**Prevention:**
```python
# WRONG — passes create_task as callback, uses wrong loop context
self.event_loop.call_soon_threadsafe(asyncio.create_task, self._process_changed_file(path))

# CORRECT option 1 — run_coroutine_threadsafe returns a Future, fire-and-forget
asyncio.run_coroutine_threadsafe(self._process_changed_file(path), self.event_loop)

# CORRECT option 2 — lambda wraps loop.create_task, targets the right loop
self.event_loop.call_soon_threadsafe(
    self.event_loop.create_task, self._process_changed_file(path)
)
```

Use `run_coroutine_threadsafe` when the watchdog thread may need to handle the result or catch exceptions. Use the `loop.create_task` lambda when fire-and-forget is acceptable. Never pass `asyncio.create_task` (the module-level function) as a callback to `call_soon_threadsafe`.

**Addresses:** "Fix real-time watcher thread bridge" in Active requirements.

---

### Pitfall 2: `asyncio.gather` with `return_exceptions=True` — silent upload failures

**What goes wrong:**
When `asyncio.gather(*tasks, return_exceptions=True)` is the replacement for the serial loop, every exception from a failed upload is collected into the results list as an exception object, not raised. If the caller does not inspect the return value and check each item for `isinstance(result, BaseException)`, all upload failures are silently swallowed. The backup daemon reports success, `.milo_backup.info` is not updated (since the upload failed before state was written), and on the next scan those files will be retried — but the operator never sees the failure until they audit S3 manually.

The alternative (`return_exceptions=False`) has the opposite problem: the first upload failure cancels the await and the caller receives only that one exception, while all other in-flight tasks keep running detached with no owner. Their exceptions become unhandled task exceptions logged to stderr under Python 3.11's `asyncio` unhandled exception handler — easy to miss in daemon log output.

**Why it happens:**
`asyncio.gather` is not a transactional batch — it is a fan-out/fan-in primitive. Its exception semantics are easily misread as "raise if any fail" when in fact `return_exceptions=False` means "raise on first fail, abandon the rest silently."

**Warning signs:**
- Upload count in stats does not match files changed
- `_stats["failed_uploads"]` stays at 0 even when S3 credentials expire
- No exception logged but S3 bucket does not contain the expected file
- Python 3.11 `Task exception was never retrieved` warnings in stderr

**Prevention:**
```python
results = await asyncio.gather(*tasks, return_exceptions=True)
for path, result in zip(file_paths, results):
    if isinstance(result, BaseException):
        logger.error("Upload failed for %s: %r", path, result)
        self._stats["failed_uploads"] += 1
    else:
        self._stats["uploaded_files"] += 1
```

Always pair `return_exceptions=True` with a result-inspection loop. Log each failure with the specific file path so failures are actionable. Do not update `.milo_backup.info` for a file whose task returned an exception.

**Addresses:** "Fix serial upload bug" in Active requirements.

---

### Pitfall 3: Hidden files and sensitive dotfiles silently uploaded to S3

**What goes wrong:**
The `_should_ignore_file` method applies ignore logic to directories (dot-prefix directories are skipped) but not to individual files with a dot prefix. Any hidden file inside a watched folder — `.env`, `.npmrc`, `.netrc`, `id_rsa`, `.aws/credentials`, `.gitconfig`, `.bash_history` — passes all ignore checks and is uploaded to S3. Because S3 buckets used for personal backups often have no encryption-at-rest policy and bucket ACLs that default to private (not public), this is frequently not an immediately visible breach, but the credentials are permanently stored in object storage without expiry.

Security researchers documented over 90,000 unique environment variable combinations with cloud credentials leaked via exposed `.env` files in 2024. The attack surface here is that if the S3 bucket or IAM key is later compromised, all secrets backed up to it are exposed at once.

**Why it happens:**
Inconsistent policy between `_should_ignore_directory` (blocks dot-prefix dirs) and `_should_ignore_file` (does not block dot-prefix files). Ignore patterns in `ignore_patterns` only use exact string matching, never glob expansion, so entries like `*.pem` never match `id_rsa.pem`.

**Warning signs:**
- `.env` files visible under watched folder paths in `aws s3 ls` output
- `ignored_files` counter always 0 (never incremented) makes it impossible to audit what was skipped
- `*.pyc` files appearing in S3 alongside Python source (confirms glob patterns never matched)

**Prevention:**
```python
# In _should_ignore_file: add dot-file default deny
if filename.startswith("."):
    return True  # consistent with _should_ignore_directory policy

# And use fnmatch for glob patterns in ignore_patterns
import fnmatch
for pattern in self.ignore_patterns:
    if "*" in pattern or "?" in pattern:
        if fnmatch.fnmatch(filename, pattern):
            return True
    elif filename == pattern:
        return True
```

Add a default deny-list of known sensitive file patterns as a non-overridable base set (applied before user-configured patterns):

```python
SENSITIVE_FILE_DENY_LIST = {
    ".env", ".env.local", ".env.production", ".env.development",
    ".netrc", ".npmrc", ".pypirc", ".bash_history", ".zsh_history",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "*.pem", "*.key", "*.p12", "*.pfx",
    "aws_credentials", "credentials",  # AWS credential files
}
```

**Addresses:** "Fix hidden file leakage" and "Fix glob patterns in ignore list" in Active requirements.

---

### Pitfall 4: Glob patterns as plain strings — broken ignore logic

**What goes wrong:**
`ignore_patterns` contains strings like `"*.pyc"`, `"*.bak"`, `"*.backup"`, `"*~"`. The `_should_ignore_file` method does `filename in self.ignore_patterns` (set membership, exact match) and `filename.startswith(".")` (prefix check). Neither check expands glob wildcards. `report.bak` is not `"*.bak"`, so it is never ignored. This means compiled Python bytecode, editor backup files, and other noise all get uploaded to S3.

This is a separate failure path from the dot-file issue: it affects non-hidden files that should be excluded by extension pattern.

**Why it happens:**
Python's `set.__contains__` is a hash lookup, not a pattern matcher. Glob patterns in a `set` are purely decorative unless explicitly applied with `fnmatch.fnmatch` or `pathlib.Path.match`.

**Warning signs:**
- `__pycache__/` contents appearing in S3 (if `__pycache__` dir is not caught by dir-ignore)
- `.bak` or `~` files from editors visible in S3

**Prevention:**
```python
import fnmatch

def _matches_any_pattern(filename: str, patterns: set[str]) -> bool:
    for pattern in patterns:
        if "*" in pattern or "?" in pattern or "[" in pattern:
            if fnmatch.fnmatch(filename, pattern):
                return True
        else:
            if filename == pattern:
                return True
    return False
```

Split patterns at construction time into two sets — exact strings and glob patterns — to avoid the per-file linear scan cost on large folders.

**Addresses:** "Fix glob patterns in ignore list" in Active requirements.

---

## Moderate Pitfalls

### Pitfall 5: Sync `open()` inside `async def` — event loop stall on slow disks

**What goes wrong:**
`_load_backup_info` and `_update_backup_info` are declared `async` but call synchronous `open()`. On an SSD with local files, a 10 KB JSON read completes in under 1 ms and the stall is imperceptible. On network-attached storage, a spinning disk, or a filesystem under memory pressure, a single `open()` can block for 50–500 ms. Since the event loop is single-threaded, that entire duration is dead time: no other coroutine runs, no upload task progresses, no watchdog events are processed. With 20 concurrent folder scans at startup, stalls compound multiplicatively.

**Why it happens:**
Python's `async def` keyword does not automatically make I/O async — it only enables the `await` syntax. A `def open()` call inside an `async def` executes synchronously on the event loop thread just as it would in non-async code.

**Warning signs:**
- Event loop lag warnings if using a monitor (e.g., `asyncio` debug mode `PYTHONASYNCIODEBUG=1`)
- Upload tasks that should be concurrent take wall-clock time proportional to folder count, not file count
- On network storage, CPU usage drops to near zero during scan phases (event loop is blocked waiting for disk)

**Prevention:**
```python
import aiofiles
import json

async def _load_backup_info(self, folder_path: Path) -> dict:
    info_file = folder_path / ".milo_backup.info"
    if not info_file.exists():
        return {}
    async with aiofiles.open(info_file, "r", encoding="utf-8") as f:
        content = await f.read()
    return json.loads(content)

async def _update_backup_info(self, folder_path: Path, info: dict) -> None:
    info_file = folder_path / ".milo_backup.info"
    async with aiofiles.open(info_file, "w", encoding="utf-8") as f:
        await f.write(json.dumps(info, indent=2))
```

`aiofiles` delegates to a thread pool internally — it is not true kernel async I/O, but it releases the event loop thread during the blocking syscall, which is sufficient for this use case.

**Addresses:** "Fix sync file I/O in async methods" in Active requirements.

---

### Pitfall 6: `.milo_backup.info` concurrent write corruption

**What goes wrong:**
Two concurrent tasks can race on `_update_backup_info` for the same folder. Scenario: a real-time watchdog event triggers `_process_current_folder` for folder A while the scheduled 5-minute scan also processes folder A. Both coroutines read the info file, compute their updates independently, and write back. The second write overwrites the first. Whichever task wrote first loses its MD5 updates, and on the next scan those files are re-hashed and potentially re-uploaded.

More destructively: if the write is non-atomic (write to the file path directly rather than write-rename), a crash mid-write leaves a truncated JSON file. On the next startup `json.loads` raises `JSONDecodeError`, and the entire folder's state is lost, triggering a full re-upload of everything in that folder.

**Why it happens:**
asyncio is cooperative — two coroutines interleave at `await` points. `aiofiles.open` contains `await` points, so a context switch between the read-modify-write steps of two concurrent `_process_current_folder` calls for the same folder is possible.

**Warning signs:**
- Files re-uploaded on every scan despite not changing
- `JSONDecodeError` on startup for `.milo_backup.info` files
- `_stats["uploaded_files"]` count higher than files actually modified

**Prevention:**
Use a per-folder `asyncio.Lock` to serialize info-file access:

```python
self._folder_locks: dict[Path, asyncio.Lock] = {}

def _get_folder_lock(self, folder: Path) -> asyncio.Lock:
    if folder not in self._folder_locks:
        self._folder_locks[folder] = asyncio.Lock()
    return self._folder_locks[folder]

async def _update_backup_info(self, folder_path: Path, info: dict) -> None:
    async with self._get_folder_lock(folder_path):
        tmp = folder_path / ".milo_backup.info.tmp"
        async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
            await f.write(json.dumps(info, indent=2))
        tmp.replace(folder_path / ".milo_backup.info")  # atomic on POSIX
```

The write-then-rename pattern (`tmp.replace(target)`) is atomic on POSIX filesystems — a crash mid-write leaves the old file intact rather than a truncated one. On Windows, `Path.replace` is not atomic but is still safer than direct overwrite.

**Addresses:** "Fix sync file I/O in async methods" indirectly; foundational for correctness of any info-file writes under concurrent load.

---

### Pitfall 7: S3 multipart upload — orphaned parts and hidden storage cost

**What goes wrong:**
When a multipart upload is interrupted (process killed, network timeout, exception during part upload), `abort_multipart_upload` is called in the exception handler. If that abort call itself fails (network gone, credentials expired, S3 throttle) or if the process receives SIGKILL before the abort runs, the uploaded parts remain in S3 as "incomplete multipart uploads." AWS charges for incomplete part storage at the same rate as complete objects. With no lifecycle rule, these parts accumulate indefinitely. A file repeatedly interrupted (e.g., a 2 GB file on a flaky connection) can accumulate parts equal to many times its actual size.

**Why it happens:**
S3 multipart uploads are not self-cleaning. `CompleteMultipartUpload` and `AbortMultipartUpload` are explicit API calls. If neither is called, AWS keeps the parts until a lifecycle rule or manual cleanup removes them.

**Warning signs:**
- S3 storage costs growing faster than actual data stored
- `aws s3api list-multipart-uploads --bucket <bucket>` returns entries
- Uploads appear to succeed (no exception logged) but object never appears in S3 (abort called after part upload but before `complete`)

**Prevention:**
Two independent defenses, both required:

1. **Code-level abort in finally block:**
```python
upload_id = None
try:
    response = await client.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = response["UploadId"]
    parts = await self._upload_parts(client, bucket, key, upload_id, file_path)
    await client.complete_multipart_upload(
        Bucket=bucket, Key=key, UploadId=upload_id,
        MultipartUpload={"Parts": parts}
    )
except Exception:
    if upload_id:
        with contextlib.suppress(Exception):
            await client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
    raise
```

2. **S3 lifecycle rule** (set once, protects against process crashes):
```json
{
  "Rules": [{
    "ID": "abort-incomplete-multipart",
    "Status": "Enabled",
    "Prefix": "",
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
  }]
}
```

This can be set programmatically at startup via `put_bucket_lifecycle_configuration`.

**Addresses:** "Fix serial upload bug" (the gather replacement makes concurrent multipart more likely, increasing exposure to this failure mode).

---

### Pitfall 8: Signal handling with `loop.add_signal_handler` — Windows crash

**What goes wrong:**
`loop.add_signal_handler(signal.SIGTERM, ...)` raises `NotImplementedError` on Windows. The project targets macOS, Linux, and Windows. Re-enabling the commented-out `_setup_signal_handlers()` call without a platform guard will cause an immediate `NotImplementedError` on every Windows startup, making the daemon unlaunchable on that platform.

A secondary issue: `signal.signal()` (the stdlib fallback) is not async-safe. It installs a raw C-level signal handler. If the signal fires while the event loop is executing an `await`, the signal handler runs synchronously in the loop thread. If the handler tries to call any asyncio API directly (e.g., `loop.stop()`), it may corrupt internal loop state. `loop.add_signal_handler` avoids this by queuing the handler into the event loop's callback queue.

**Why it happens:**
`add_signal_handler` is implemented only in `asyncio.SelectorEventLoop` (Unix). Windows uses `asyncio.ProactorEventLoop`, which does not implement it.

**Warning signs:**
- `NotImplementedError` on Windows immediately at startup when signal handler code is reached
- Graceful shutdown not triggered on SIGTERM on Unix when the call is commented out
- In-flight uploads not flushed on process termination

**Prevention:**
```python
import sys
import signal

def _setup_signal_handlers(self) -> None:
    if sys.platform == "win32":
        # ProactorEventLoop does not support add_signal_handler.
        # Use signal.signal() with a thread-safe flag.
        signal.signal(signal.SIGINT, lambda sig, frame: self._request_shutdown())
        signal.signal(signal.SIGTERM, lambda sig, frame: self._request_shutdown())
    else:
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, self._request_shutdown)
        loop.add_signal_handler(signal.SIGINT, self._request_shutdown)

def _request_shutdown(self) -> None:
    # asyncio-safe: schedule stop on the event loop
    self.loop.call_soon_threadsafe(self.loop.stop)
```

Do not call `loop.stop()` directly from a Unix signal handler registered with `signal.signal` — use `loop.add_signal_handler` or `loop.call_soon_threadsafe` to queue the stop.

**Addresses:** "Re-enable signal handling in headless mode" in Active requirements.

---

## Minor Pitfalls

### Pitfall 9: `asyncio.get_event_loop()` deprecation in Python 3.10+

**What goes wrong:**
`asyncio.get_event_loop()` called from inside a coroutine without a running loop emits `DeprecationWarning` in Python 3.10 and will raise `RuntimeError` in a future Python version. `S3Manager._calculate_md5` uses this call to get the executor. In Python 3.11, if called when no loop is set for the current thread, it creates a new loop implicitly — which then conflicts with the loop created by `asyncio.run()`. The failure mode is a warning today, a crash in a future Python release.

**Prevention:**
```python
# Replace
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(None, compute_md5, path)

# With
loop = asyncio.get_running_loop()
result = await loop.run_in_executor(None, compute_md5, path)
```

`asyncio.get_running_loop()` raises `RuntimeError` immediately if there is no running loop, making bugs explicit rather than silently creating a new orphaned loop.

---

### Pitfall 10: Semaphore not wired to config — false confidence in concurrency control

**What goes wrong:**
`upload_semaphore = asyncio.Semaphore(50)` is hardcoded. `config.yaml` shows `max_concurrent_uploads: 100`. Users who set this value to tune concurrency observe no change in behavior. If `max_concurrent_uploads` is set very high (e.g., 500) to match a high-bandwidth link, the actual limit remains 50 — the upload throughput plateau is misattributed to network or S3 capacity rather than the semaphore. Conversely, if set to 5 to avoid throttling, the application ignores that constraint.

**Prevention:**
```python
# In FileListener.__init__
self.upload_semaphore = asyncio.Semaphore(config.max_concurrent_uploads)
self.md5_semaphore = asyncio.Semaphore(config.max_concurrent_uploads)
```

Note: with the serial upload bug still present, the effective concurrency is 1 regardless. This fix only has observable effect after the `asyncio.gather` fix is applied.

**Addresses:** "Wire `max_concurrent_uploads` config to semaphore" in Active requirements.

---

### Pitfall 11: Diverging ignore pattern sets — watcher vs. scanner inconsistency

**What goes wrong:**
`FileListener.ignore_patterns` and `FileChangeHandler.ignore_patterns` are defined independently in two files. When a new pattern is added to one, the other is not updated. This creates a class of silent backup inconsistency: a file like `$RECYCLE.BIN` is excluded from real-time events (in `FileChangeHandler`) but not from batch scans (in `FileListener`), or vice versa. The user cannot reason about what will or will not be backed up from any single source of truth.

**Prevention:**
Extract to a shared constant in a dedicated module:

```python
# aws_copier/core/ignore_config.py
IGNORE_PATTERNS: frozenset[str] = frozenset({
    "*.pyc", "*.bak", "*.backup", "*~",
    ".DS_Store", "Thumbs.db", "desktop.ini",
    "$RECYCLE.BIN", "System Volume Information",
})

IGNORE_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", "node_modules",
    ".venv", "venv", ".tox",
})
```

Both `FileListener` and `FileChangeHandler` import from this module. Changes are made in one place.

**Addresses:** "Deduplicate ignore patterns" in Active requirements.

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| Replace serial loop with `gather` | Silent exception swallowing (Pitfall 2) | Always inspect `return_exceptions=True` results; log per-file failures |
| Fix thread bridge in watchdog | Wrong-loop RuntimeError (Pitfall 1) | Use `run_coroutine_threadsafe`; test on Python 3.10+ specifically |
| Fix ignore patterns with fnmatch | Sensitive files still leaked if dot-file deny not added (Pitfall 3) | Add dot-file deny alongside glob fix; the two bugs share code paths |
| Re-enable signal handling | Windows crash on startup (Pitfall 8) | Platform guard required before re-enabling |
| Switch info file to aiofiles | Race condition on concurrent folder scans (Pitfall 6) | Add per-folder `asyncio.Lock` at the same time as the aiofiles change |
| Concurrent multipart uploads | Orphaned parts accumulating (Pitfall 7) | Add S3 lifecycle rule at project setup, not per-upload |
| Wire semaphore to config | No visible effect until serial bug also fixed | Sequence: fix gather first, then wire semaphore |

---

## Sources

- Python asyncio official docs — `run_coroutine_threadsafe`: https://docs.python.org/3/library/asyncio-task.html#asyncio.run_coroutine_threadsafe
- Python asyncio official docs — `add_signal_handler` (Unix only): https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.add_signal_handler
- Python asyncio official docs — `gather` semantics: https://docs.python.org/3/library/asyncio-task.html#asyncio.gather
- `call_soon_threadsafe` threading issue: https://github.com/MagicStack/uvloop/issues/408
- AWS docs — abort incomplete multipart uploads lifecycle: https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpu-abort-incomplete-mpu-lifecycle-config.html
- AWS blog — S3 multipart upload cost from orphaned parts: https://aws.amazon.com/blogs/aws-cloud-financial-management/discovering-and-deleting-incomplete-multipart-uploads-to-lower-amazon-s3-costs/
- Exposed .env files threat research 2024: https://www.helpnetsecurity.com/2024/08/15/exposed-environment-files-data-theft/
- asyncio exception handling patterns: https://piccolo-orm.com/blog/exception-handling-in-asyncio/
- asyncio semaphore pitfalls: https://runebook.dev/en/docs/python/library/asyncio-sync/asyncio.Semaphore
- aiofiles library: https://pypi.org/project/aiofiles/
