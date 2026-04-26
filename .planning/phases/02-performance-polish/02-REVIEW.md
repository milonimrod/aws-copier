---
phase: 02-performance-polish
reviewed: 2026-04-26T00:00:00Z
depth: standard
files_reviewed: 11
files_reviewed_list:
  - aws_copier/core/file_listener.py
  - aws_copier/core/folder_watcher.py
  - aws_copier/core/s3_manager.py
  - aws_copier/models/simple_config.py
  - main.py
  - pyproject.toml
  - tests/unit/test_file_listener.py
  - tests/unit/test_folder_watcher.py
  - tests/unit/test_main_lifecycle_wiring.py
  - tests/unit/test_s3_manager_perf.py
  - tests/unit/test_simple_config.py
findings:
  critical: 1
  warning: 5
  info: 4
  total: 10
status: issues_found
---

# Phase 02: Code Review Report

**Reviewed:** 2026-04-26
**Depth:** standard
**Files Reviewed:** 11
**Status:** issues_found

## Summary

Phase 2 introduced a substantial set of correctness and performance improvements: per-folder asyncio locks, an in-memory backup-info cache with mtime-gated invalidation, concurrent upload gather, debounced watchdog events, PERF-03 precomputed MD5 pass-through, CONFIG-05 credential chain detection, and CONFIG-07 lifecycle-rule management. The architecture is well-layered and the test coverage is thorough. Most of the design decisions are sound.

One critical security issue was found: AWS secret key material is exposed in plain text by `SimpleConfig.to_dict()` and is therefore propagated into every JSON log line that calls `logger.info(f"... {stats}")` on the config dictionary. Five warnings cover logic correctness issues — a double-close bug in `S3Manager.close()`, a race window in the cache-invalidation path of `_update_backup_info`, a double-MD5 computation in `_upload_single_file`, an unsafe `asyncio.Event()` construction site in `AWSCopierApp.__init__`, and `shutdown()` re-entrancy being fragile under concurrent signals. Four info items cover code quality.

---

## Critical Issues

### CR-01: AWS secret key exposed via `to_dict()` and `print()` in s3_manager main harness

**File:** `aws_copier/models/simple_config.py:96-105` and `aws_copier/core/s3_manager.py:526`

**Issue:** `SimpleConfig.to_dict()` includes both `aws_access_key_id` and `aws_secret_access_key` in the returned dict. The `main()` test harness in `s3_manager.py` calls `print("Loaded config:", config.to_dict())` (line 526), which prints the secret key to stdout. More broadly, any caller that serialises the config dict to a log file or monitoring system inadvertently leaks the secret. Although `to_dict()` is used legitimately in `save_to_yaml`, its public API surface invites accidental exposure.

**Fix:**
```python
# simple_config.py — redact secret in to_dict(); add a separate _to_yaml_dict() for saving
def to_dict(self) -> Dict[str, Any]:
    """Public representation — secret key is redacted."""
    return {
        "aws_access_key_id": self.aws_access_key_id,
        "aws_secret_access_key": "***REDACTED***" if self.aws_secret_access_key else "",
        "aws_region": self.aws_region,
        "s3_bucket": self.s3_bucket,
        "s3_prefix": self.s3_prefix,
        "watch_folders": {str(fp): n for fp, n in self.folder_s3_mapping.items()},
        "max_concurrent_uploads": self.max_concurrent_uploads,
    }

def _to_yaml_dict(self) -> Dict[str, Any]:
    """Internal full representation for YAML serialisation."""
    d = self.to_dict()
    d["aws_secret_access_key"] = self.aws_secret_access_key  # real value for file write
    return d

def save_to_yaml(self, config_path: Path) -> None:
    data = self._to_yaml_dict()
    ...
```

For `s3_manager.py:526`, remove or guard the debug print:
```python
# Remove this line from the module-level main() harness:
# print("Loaded config:", config.to_dict())
logger.debug("Config loaded (credentials redacted): %s", config.to_dict())
```

---

## Warnings

### WR-01: Double-close of S3 client in `S3Manager.close()`

**File:** `aws_copier/core/s3_manager.py:173-181`

**Issue:** `close()` first calls `await self._s3_client.close()` on line 176, then calls `await self._exit_stack.aclose()` on line 179. The `AsyncExitStack` was entered with the client via `enter_async_context`, so `aclose()` will call `__aexit__` on the aiobotocore client a second time, producing a warning or error from aiobotocore about closing an already-closed client.

```python
async def close(self) -> None:
    if self._s3_client:
        await self._s3_client.close()   # first close
        self._s3_client = None
    if self._exit_stack:
        await self._exit_stack.aclose()  # second close via __aexit__ of the same client
        self._exit_stack = None
```

**Fix:** Remove the explicit `self._s3_client.close()` call; rely solely on the `AsyncExitStack` to close the client cleanly:
```python
async def close(self) -> None:
    """Close the S3 manager and cleanup resources."""
    if self._exit_stack:
        await self._exit_stack.aclose()
        self._exit_stack = None
    self._s3_client = None
    logger.debug("S3Manager closed")
```

### WR-02: Cache updated inside the lock but disk mtime invalidated — next reader may see stale data under concurrent access

**File:** `aws_copier/core/file_listener.py:619-631`

**Issue:** `_update_backup_info` acquires the per-folder lock, writes to disk via `aiofiles`, then updates `_backup_info_cache` and pops `_backup_info_mtime`. However, the OS-assigned mtime is NOT re-read after the write, so when another coroutine calls `_load_backup_info` after the lock is released, it will call `stat()` and find a new mtime (the one the OS just assigned) that is absent from `_backup_info_mtime`, treating this as a cache miss and re-reading the file from disk. This is benign in isolation, but under the documented "concurrent scan + real-time event hitting the same folder" scenario described in the ASYNC-03 comment, the cache will always be cold after every write, defeating PERF-02 for the scan cycle that immediately follows an upload. A more subtle risk: if the file-system timestamp resolution is coarse (FAT32, 2-second granularity), two back-to-back writes within the same tick produce the same mtime; if the cache is then primed with the first write's data and the second write happens before `stat()` is called by the reader, the reader will incorrectly consider the cache fresh.

**Fix:** After the aiofiles write, stat the file inside the lock and store the new mtime in `_backup_info_mtime` to prime the cache for the immediately following reader:
```python
async with self._get_folder_lock(backup_info_file.parent):
    async with aiofiles.open(backup_info_file, "w", encoding="utf-8") as f:
        await f.write(json.dumps(backup_info, indent=2))
    # Prime cache with the actual on-disk mtime so the next reader gets a cache hit.
    try:
        new_mtime = backup_info_file.stat().st_mtime
        self._backup_info_mtime[backup_info_file.parent] = new_mtime
    except OSError:
        self._backup_info_mtime.pop(backup_info_file.parent, None)
    self._backup_info_cache[backup_info_file.parent] = backup_files
```

### WR-03: Double MD5 computation in `_upload_single_file` — PERF-03 benefit lost for the common path

**File:** `aws_copier/core/file_listener.py:456-483`

**Issue:** `_upload_single_file` calls `self._calculate_md5(file_path)` (line 458) to compute `local_md5`, then passes the file path and the S3 key to `self.s3_manager.upload_file(file_path, s3_key)` (line 483) — but does NOT forward `local_md5` as `precomputed_md5`. `S3Manager.upload_file` therefore computes the MD5 a second time internally (line 207). This makes the `precomputed_md5` parameter added in PERF-03 completely ineffective on the normal upload path.

**Fix:**
```python
# _upload_single_file, line 483 — pass the already-computed hash:
if await self.s3_manager.upload_file(file_path, s3_key, precomputed_md5=local_md5):
```

### WR-04: `asyncio.Event()` constructed before the event loop is running in `AWSCopierApp.__init__`

**File:** `main.py:30`

**Issue:** `self.shutdown_event = asyncio.Event()` is called inside `__init__`, which runs synchronously before `asyncio.run(main())` starts the event loop. On Python 3.10+, `asyncio.Event()` attaches itself to the running loop at construction time. When there is no running loop (i.e., the object is constructed before `asyncio.run()`), Python 3.10+ emits a deprecation warning and Python 3.12+ raises `DeprecationWarning` (soon to be an error). Under the `sync_main()` path the `AWSCopierApp()` constructor is called inside `asyncio.run(main())` so the loop is running, but it is called before `app.start()` is awaited — the exact timing depends on the current Python version's event-loop binding semantics.

**Fix:** Move `asyncio.Event()` construction into the `start()` coroutine where the event loop is guaranteed to be running, or accept it as an argument:
```python
async def start(self) -> None:
    self.shutdown_event = asyncio.Event()
    ...
```

Or create it lazily on first use:
```python
@property
def _shutdown_event(self) -> asyncio.Event:
    if self._shutdown_event_obj is None:
        self._shutdown_event_obj = asyncio.Event()
    return self._shutdown_event_obj
```

### WR-05: `shutdown()` re-entrancy guard checks `self.running` but signal handler sets `self.running = False` before `shutdown()` is called

**File:** `main.py:84-120`

**Issue:** The shutdown guard at line 87 (`if not self.running: return`) relies on `self.running` being `True` when `shutdown()` is first called. However, `_handle_signal` (line 161) sets `self.running = False` and then sets `self.shutdown_event`, which causes the `while self.running` loop to break and fall into the `finally: await self.shutdown()` block. At that point `self.running` is already `False`, so the guard fires immediately and `shutdown()` returns without stopping the watcher or closing the S3 client. The intended double-call guard (`shutdown() from signal AND from finally`) thus silently skips all cleanup on signal-triggered shutdown.

**Fix:** Use a dedicated `_shutting_down` flag rather than overloading `running`:
```python
def __init__(self):
    ...
    self._shutting_down = False

async def shutdown(self) -> None:
    if self._shutting_down:
        return
    self._shutting_down = True
    self.running = False
    ...  # rest of cleanup

async def _handle_signal(self, signum: int) -> None:
    logger.info(f"Received signal {signum}; initiating graceful shutdown")
    self.shutdown_event.set()
    # Do NOT set self.running here; let shutdown() do it atomically.
```

---

## Info

### IN-01: `save_to_yaml` opens the config file without explicit encoding

**File:** `aws_copier/models/simple_config.py:81`

**Issue:** `with open(config_path, "w") as f:` uses the platform default encoding. On Windows with non-UTF-8 locale, paths containing non-ASCII characters could be mangled or raise `UnicodeEncodeError`. The corresponding `load_from_yaml` on line 62 also omits `encoding=`.

**Fix:**
```python
with open(config_path, "w", encoding="utf-8") as f:
    yaml.dump(data, f, ...)

# and in load_from_yaml:
with open(config_path, encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}
```

### IN-02: `pyproject.toml` declares `ruff` as a runtime dependency and `python-dotenv` only in the dev group

**File:** `pyproject.toml:12` and `pyproject.toml:91`

**Issue:** `ruff>=0.12.11` appears in `[project.dependencies]` (runtime), which means it is installed for every user of the package — a linter should be a dev dependency only. Conversely, `python-dotenv` is only in `[dependency-groups] dev` yet the production `s3_manager.main()` harness calls `from dotenv import load_dotenv` — although that harness is guarded by `if __name__ == "__main__"` so it doesn't affect normal operation, the import itself will fail at runtime if `python-dotenv` is not installed.

**Fix:** Move `ruff` to `[project.optional-dependencies] dev` and add `python-dotenv` there as well (or guard the import inside the `if __name__ == "__main__"` block with a try/except).

### IN-03: Emoji characters in log messages in production code

**File:** `main.py:39,55,59,62,67,118,119` and `aws_copier/core/folder_watcher.py:158,163`

**Issue:** Log messages contain emoji (e.g., `"✅ S3 Manager initialized"`, `"📁 File {event_type}: {file_path}"`). Many log aggregation systems (Splunk, CloudWatch, syslog on older platforms) either strip, corrupt, or fail to index non-ASCII characters in log records. This is particularly risky on Windows with a non-UTF-8 console or file handler.

**Fix:** Remove emoji from production log messages; use plain ASCII strings:
```python
logger.info("S3 Manager initialized")
logger.info(f"File {event_type}: {file_path}")
```

### IN-04: `test_debounce` fixture uses deprecated `asyncio.get_event_loop()` (Python 3.10+ warns)

**File:** `tests/unit/test_folder_watcher.py:494`

**Issue:** `loop = asyncio.get_event_loop()` is called inside a pytest-asyncio fixture (`debounce_handler`). In Python 3.10+, calling `get_event_loop()` outside a running event loop emits a `DeprecationWarning`; in 3.12+ it raises `RuntimeError` when no current event loop exists. Since pytest-asyncio with `asyncio_mode = "auto"` creates a new loop per test, this will at minimum generate warnings in CI output and may fail on future Python versions.

**Fix:**
```python
loop = asyncio.get_running_loop()
```

---

_Reviewed: 2026-04-26_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
