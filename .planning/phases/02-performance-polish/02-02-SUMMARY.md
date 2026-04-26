---
phase: 02-performance-polish
plan: 02
subsystem: file-listener
tags:
  - performance
  - mtime-skip
  - cache
  - backup-info-format
  - PERF-01
  - PERF-02
dependency_graph:
  requires:
    - 02-01 (any core-reliability baseline)
  provides:
    - mtime-skip scan path in FileListener
    - in-memory backup info cache (PERF-02)
    - new {md5, mtime} backup info format (D-03)
    - silent old-format migration (_migrate_entry)
  affects:
    - aws_copier/core/file_listener.py
    - tests/unit/test_file_listener.py
    - Plan 03 (PERF-03 in S3Manager — has clean {md5} foundation to build on)
    - Plan 05 (CONFIG-06 in _scan_current_files — existing_backup_info parameter now present)
tech_stack:
  added: []
  patterns:
    - mtime equality check (float st_mtime, no truncation) for file-skip decision
    - per-folder in-memory dict cache with disk-mtime invalidation
    - TDD RED/GREEN/REFACTOR across both tasks
key_files:
  created: []
  modified:
    - aws_copier/core/file_listener.py
    - tests/unit/test_file_listener.py
decisions:
  - "_upload_files return type changed from List[str] to Dict[str, float] to thread upload_mtime through the caller chain"
  - "_scan_current_files now accepts Optional[existing_backup_info] parameter with default {} for backward compatibility"
  - "_upload_single_file returns Tuple[bool, float] — _upload_with_timeout updated to Tuple[str, bool, float]"
  - "Existing tests updated to expect new {md5, mtime} dict format throughout"
metrics:
  duration: "9m 2s"
  completed: "2026-04-26T05:48:07Z"
  tasks_completed: 2
  tasks_total: 2
  files_modified: 2
---

# Phase 02 Plan 02: mtime-skip + in-memory backup info cache (PERF-01/PERF-02) Summary

**One-liner:** Mtime-based MD5 skip in `_scan_current_files` plus in-memory `.milo_backup.info` cache with disk-mtime invalidation, silently migrating the on-disk format from `{filename: "md5"}` to `{filename: {md5, mtime}}`.

## What Was Built

### New Helpers / Fields

| Name | Location | Purpose |
|------|----------|---------|
| `_backup_info_cache` | `FileListener.__init__` | `Dict[Path, Dict[str, Any]]` — in-memory cache of `.milo_backup.info` contents keyed by folder Path |
| `_backup_info_mtime` | `FileListener.__init__` | `Dict[Path, float]` — cached `st_mtime` of `.milo_backup.info` on disk; used for cache hit/miss |
| `_migrate_entry(value)` | `FileListener` | Converts old string entries `"md5hash"` → `{"md5": "md5hash", "mtime": 0.0}` on read (D-01) |

### Method Changes

#### `_load_backup_info` (rewritten)
- Return type: `Dict[str, Dict[str, Any]]` (was `Dict[str, str]`)
- **PERF-02 cache:** Checks `st_mtime` of `.milo_backup.info` before reading disk. Cache hit returns in-memory data. Cache miss reads, parses, migrates, and updates cache.
- **D-01 migration:** All entries pass through `_migrate_entry` — old string values become `{md5, mtime: 0.0}`, new dict values pass through unchanged.
- Holds per-folder `asyncio.Lock` during the entire stat + read + cache-update sequence (ASYNC-03).

#### `_update_backup_info` (rewritten)
- Parameter type: `backup_files: Dict[str, Dict[str, Any]]` (was `Dict[str, str]`)
- Return type: `bool` (was `None`) — `True` on success, `False` on error.
- **PERF-02 cache update:** After successful disk write, updates `_backup_info_cache[folder]` and pops `_backup_info_mtime[folder]` to force re-stat on next read.

#### `_scan_current_files` (rewritten)
- New signature: `async def _scan_current_files(self, folder_path, existing_backup_info=None) -> Dict[str, Dict[str, Any]]`
- **PERF-01 mtime-skip:** For each file, calls `file_path.stat()` and compares `st_mtime` against `existing_backup_info[name]["mtime"]`. Match → carry forward existing entry, increment `_stats["skipped_files"]`, skip MD5. Miss → schedule MD5 computation.
- Returns `Dict[str, Dict[str, Any]]` with `{"md5": str, "mtime": float}` entries (scan-time mtime for new files; upload_mtime will override for uploaded files via caller).

#### `_upload_single_file` (signature change)
- Return type: `Tuple[bool, float]` (was `bool`)
- Captures `upload_mtime = file_path.stat().st_mtime` immediately before `s3_manager.upload_file(...)` (D-02: a file modified during upload is detected on the next cycle).
- On `check_exists` skip, captures mtime at that point for recording.
- Returns `(False, 0.0)` on any failure.

#### `_upload_with_timeout` (signature change)
- Return type: `Tuple[str, bool, float]` — propagates `upload_mtime` from `_upload_single_file`.

#### `_upload_files` (return type change)
- Return type: `Dict[str, float]` (was `List[str]`) — maps successfully uploaded `filename -> upload_mtime`.
- `_process_current_folder` uses this dict to write `{"md5": current_md5, "mtime": upload_mtimes[filename]}` to backup info for each uploaded file.

#### `_determine_files_to_upload` (updated)
- Handles both bare MD5 strings and new `{md5, mtime}` dict entries in both `current_files` and `existing_backup_info` — extracts `["md5"]` from dicts for comparison.

#### `_process_current_folder` (updated)
- Passes `existing_backup_info` to `_scan_current_files` to enable mtime-skip.
- Unpacks `upload_mtimes` dict from `_upload_files`; builds backup entries with `upload_mtimes[filename]` (D-02).

## Test File Additions

### `TestBackupInfoMigrationAndCache` (7 tests — all pass)
| Test | What it proves |
|------|----------------|
| `test_load_migrates_old_string_format` | Old `"md5hash"` string → `{"md5": "md5hash", "mtime": 0.0}` on load (D-01) |
| `test_load_preserves_new_dict_format` | New dict entries pass through unchanged |
| `test_load_returns_empty_when_file_missing` | Missing file returns `{}` |
| `test_load_uses_cache_when_disk_mtime_unchanged` | Second call with same disk mtime hits cache, no `aiofiles.open` (PERF-02) |
| `test_load_re_reads_when_disk_mtime_changes` | Mutated disk file triggers re-read |
| `test_update_writes_dict_format` | `_update_backup_info` persists `{md5, mtime}` dict values (D-03) |
| `test_update_invalidates_cache` | Cache reflects updated content after `_update_backup_info` (PERF-02 correctness) |

### `TestMtimeSkip` (4 tests — all pass)
| Test | What it proves |
|------|----------------|
| `test_unchanged_file_skips_md5` | Second scan with unchanged files: `_calculate_md5` not called, `skipped_files` incremented (PERF-01) |
| `test_mtime_change_triggers_upload` | Modified file: `_calculate_md5` and `upload_file` both called |
| `test_first_run_after_migration_recomputes` | Migrated `mtime=0.0` entry: `_calculate_md5` called (mtime mismatch forces re-stat); post-write format is new dict |
| `test_stored_mtime_is_pre_upload_capture` | Stored mtime ≤ mtime at upload-call time (D-02 correctness) |

**Total tests in file: 49 pass. Full suite: 155 pass.**

### Updated Existing Tests
- `test_backup_info_contains_correct_md5`: now asserts `root_data["files"]["file1.txt"]["md5"] == expected_md5`
- `TestFileListenerBackupInfo::test_load_backup_info_existing_file`: updated to expect migrated `{md5, mtime: 0.0}` dict
- `TestFileListenerBackupInfo::test_update_backup_info`: passes new dict-format files to `_update_backup_info`
- `TestFileListenerAsyncBackupIO` tests: updated for new format
- `TestFileListenerUploads` tests: updated for `Tuple[bool, float]` / `Dict[str, float]` return types
- `TestFileListenerOperations::test_scan_current_files`: asserts `["md5"]` key in result

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Format mismatch broke `_process_current_folder` and `_determine_files_to_upload`**
- **Found during:** Task 1 GREEN phase — `test_incremental_scan_skips_unchanged_files` failed
- **Issue:** `_load_backup_info` now returns `Dict[str, Dict]` but `_scan_current_files` still returned bare strings; `_determine_files_to_upload` compared string != dict causing every file to appear changed on every cycle
- **Fix:** Updated `_determine_files_to_upload` to extract `["md5"]` from dict entries; updated `_process_current_folder` to normalise entries to new dict format when writing; later superseded by Task 2 changes that made `_scan_current_files` return dicts natively
- **Files modified:** `aws_copier/core/file_listener.py`
- **Commit:** f8570f3

**2. [Rule 1 - Bug] `_upload_files` return type change required caller chain updates**
- **Found during:** Task 2 GREEN phase — multiple existing tests failed due to `Dict` vs `List` mismatch
- **Issue:** Changing `_upload_files` to return `Dict[str, float]` broke tests asserting `uploaded_files[0]` (integer index on dict) and tests checking list membership
- **Fix:** Updated 5 tests in `TestFileListenerUploads` and `TestFileListenerConcurrentUpload` to use dict semantics; updated `_process_current_folder` to use `list(upload_mtimes.keys())`
- **Files modified:** `tests/unit/test_file_listener.py`
- **Commit:** a4694a0

## Downstream Plan Foundation

- **Plan 03 (PERF-03 — MD5 deduplication in S3Manager):** `_upload_single_file` already computes `local_md5` once and holds it. Adding `precomputed_md5` to `S3Manager.upload_file` is a clean additive change.
- **Plan 05 (CONFIG-06 — `.backupignore` in `_scan_current_files`):** The new `existing_backup_info` parameter is already present in `_scan_current_files`. CONFIG-06 adds `PathSpec` filtering to the same per-file loop — no signature conflicts.
- **Correctness:** No code path now writes a bare string into the dict passed to `_update_backup_info`. Every value is `{"md5": str, "mtime": float}`.

## Self-Check

### Files exist:
- `aws_copier/core/file_listener.py` — FOUND
- `tests/unit/test_file_listener.py` — FOUND
- `.planning/phases/02-performance-polish/02-02-SUMMARY.md` — FOUND (this file)

### Commits exist:
- `b742d8a` — test(02-02): RED phase Task 1
- `f8570f3` — feat(02-02): Task 1 GREEN phase
- `b485bc3` — test(02-02): RED phase Task 2
- `a4694a0` — feat(02-02): Task 2 GREEN phase

## Self-Check: PASSED

All commits verified in git log. All 49 file-listener tests pass. Full suite 155 tests pass. No unexpected file deletions. All acceptance criteria met.
