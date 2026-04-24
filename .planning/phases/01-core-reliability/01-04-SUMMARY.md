---
phase: 01-core-reliability
plan: "04"
subsystem: file-listener
tags: [async, concurrency, ignore-rules, aiofiles, gather, semaphore, state-io]
dependency_graph:
  requires:
    - "01-01"  # IGNORE_RULES singleton (ignore_rules.py) consumed here
  provides:
    - FileListener._active_upload_tasks  # consumed by Plan 05 (ASYNC-06 shutdown drain)
    - FileListener.upload_semaphore       # consumed by Plan 05 (no behavior change needed)
  affects:
    - aws_copier/core/file_listener.py
    - tests/unit/test_file_listener.py
tech_stack:
  added:
    - asyncio.gather with return_exceptions=True (fan-out upload pattern)
    - per-folder asyncio.Lock registry (read-modify-write serialisation)
    - Set[asyncio.Task] active task tracking (shutdown drain hook)
  patterns:
    - _upload_with_timeout wraps coroutine before create_task (Pitfall 1 fix from RESEARCH.md)
    - _get_folder_lock returns same Lock instance per folder path (dict-backed registry)
key_files:
  modified:
    - aws_copier/core/file_listener.py
    - tests/unit/test_file_listener.py
decisions:
  - "Per-folder Lock held only for aiofiles I/O scope, not for json.loads/json.dumps, keeping contention window minimal (T-04-06 accept)"
  - "_upload_with_timeout wraps the coroutine (not the task) so each file's 300s window starts concurrently, not serially"
  - "Removed _should_ignore_file and _should_ignore_directory entirely — IGNORE_RULES.should_ignore_file covers dot-files, sensitive patterns, and glob patterns in a single authoritative source"
  - "test_upload_files_success uses set comparison (not ordered list) — asyncio.gather does not guarantee result order"
metrics:
  duration: "~20 minutes"
  completed_date: "2026-04-24"
  tasks_completed: 2
  files_modified: 2
  tests_added: 9
  tests_removed: 2
  total_tests_after: 135
---

# Phase 01 Plan 04: FileListener Concurrent Refactor Summary

**One-liner:** Replaced serial upload loop with asyncio.gather fan-out, converted backup-state I/O to aiofiles under per-folder Lock, wired semaphore to config, and deleted duplicate ignore logic in favour of IGNORE_RULES singleton.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Apply ASYNC-02/03/04, CONFIG-01, IGNORE-03/04 to file_listener.py | 8e46afb | aws_copier/core/file_listener.py |
| 2 | Extend test_file_listener.py with 9 behaviour-proving tests | ced9b32 | tests/unit/test_file_listener.py |

## Edits Applied to file_listener.py (All 10)

| Edit | Requirement | Description |
|------|-------------|-------------|
| EDIT 1 | IGNORE-03 | Added `IGNORE_RULES` import and `Set`, `Tuple` to typing imports |
| EDIT 2 | CONFIG-01 / ASYNC-03 / ASYNC-04 / ASYNC-06 | Replaced `__init__` body: config-wired semaphore, `_folder_locks` dict, `_active_upload_tasks` set, ASYNC-04 comment; deleted `ignore_patterns` and `ignore_dirs` blocks |
| EDIT 3 | ASYNC-03 | Added `_get_folder_lock(folder_path)` sync helper (dict-backed registry) |
| EDIT 4 | IGNORE-03 | Replaced `_should_ignore_directory` call sites in `_process_folder_recursively` with `IGNORE_RULES.should_ignore_dir` |
| EDIT 5 | ASYNC-03 | Rewrote `_load_backup_info` to use `aiofiles.open` under `_get_folder_lock` |
| EDIT 6 | IGNORE-03 / IGNORE-04 | Replaced `_should_ignore_file` call in `_scan_current_files` with `IGNORE_RULES.should_ignore_file` + `_stats["ignored_files"] += 1` |
| EDIT 7 | ASYNC-02 | Added `_upload_with_timeout` coroutine helper wrapping `asyncio.wait_for(_upload_single_file(...), timeout=300)` |
| EDIT 8 | ASYNC-02 | Rewrote `_upload_files` to use `asyncio.gather(*tasks, return_exceptions=True)` fan-out; tasks added to `_active_upload_tasks` with done-callback discard |
| EDIT 9 | ASYNC-03 | Rewrote `_update_backup_info` to use `aiofiles.open` under `_get_folder_lock` |
| EDIT 10 | IGNORE-03 | Deleted `_should_ignore_file` and `_should_ignore_directory` methods (lines 466-515 in original) |

## Plan 05 Consumer Interface

```python
# ASYNC-06 shutdown drain (Plan 05) reads:
file_listener._active_upload_tasks: Set[asyncio.Task]
# Tasks are added when created in _upload_files, discarded via done_callback
# After gather() completes, set is always empty (tasks are done)
# During gather() (i.e., shutdown race), set contains in-flight upload tasks

file_listener.upload_semaphore  # asyncio.Semaphore(config.max_concurrent_uploads)
# No behavior change needed for drain — semaphore already limits concurrency
```

## Concurrency Test Proof (ASYNC-02)

`test_upload_files_runs_concurrently`: 10 files each with 0.2s mock upload delay.

- Serial expected time: 10 × 0.2s = 2.0s
- Actual measured: **0.61s total test suite** (all 38 tests including concurrency test)
- Assertion threshold: `< 1.0s` — passes with significant headroom

The timing proves the gather fan-out is genuinely concurrent, not serial.

## Test Count Delta

| Category | Before | After | Delta |
|----------|--------|-------|-------|
| Tests in test_file_listener.py | 29 | 38 | +9 |
| Removed (obsolete ignore-method tests) | — | — | -2 |
| Net addition | — | — | +7 |
| Total suite | 128 | 135 | +7 |

### New Tests Added

- `TestFileListenerConfig::test_config_max_concurrent_uploads_wires_to_semaphore` (CONFIG-01)
- `TestFileListenerAsyncBackupIO::test_load_backup_info_uses_aiofiles_and_lock` (ASYNC-03)
- `TestFileListenerAsyncBackupIO::test_update_backup_info_uses_aiofiles_and_lock` (ASYNC-03)
- `TestFileListenerAsyncBackupIO::test_folder_lock_is_same_instance_per_folder` (ASYNC-03)
- `TestFileListenerConcurrentUpload::test_upload_files_runs_concurrently` (ASYNC-02)
- `TestFileListenerConcurrentUpload::test_upload_files_active_tasks_tracked` (ASYNC-06 hook)
- `TestFileListenerConcurrentUpload::test_upload_files_gather_handles_exceptions` (T-04-08)
- `TestFileListenerIgnoreIntegration::test_file_listener_has_no_local_ignore_attrs` (IGNORE-03)
- `TestFileListenerIgnoreIntegration::test_scan_increments_ignored_files_stat` (IGNORE-04)

### Tests Removed

- `TestFileListenerUtilities::test_should_ignore_file` — methods deleted; equivalent coverage in `tests/unit/test_ignore_rules.py` (Plan 01)
- `TestFileListenerUtilities::test_should_ignore_directory` — same reason

## Deviations from Plan

### Auto-fixes

**1. [Rule 1 - Bug] Fixed ordered list assertion in test_upload_files_success**
- **Found during:** Task 2
- **Issue:** Original test asserted `uploaded_files == ["file1.txt", "file2.txt"]` (ordered). With `asyncio.gather` the result order follows task-creation order but this is an implementation detail, not a contract.
- **Fix:** Changed to `set(uploaded_files) == {"file1.txt", "file2.txt"}` — content-only assertion.
- **Files modified:** tests/unit/test_file_listener.py
- **Commit:** ced9b32

No other deviations — plan executed as written.

## Known Stubs

None. All functionality is fully wired.

## Threat Flags

No new security-relevant surface introduced beyond what the plan's threat model covers.

## Self-Check: PASSED

- aws_copier/core/file_listener.py: FOUND
- tests/unit/test_file_listener.py: FOUND
- Commit 8e46afb: FOUND
- Commit ced9b32: FOUND
- All 135 tests pass (uv run pytest --no-cov -q)
- ruff check + format: both pass on modified files
