---
phase: 02-performance-polish
plan: "04"
subsystem: folder-watcher
tags:
  - performance
  - debounce
  - asyncio
  - folder-watcher
  - PERF-04
dependency_graph:
  requires:
    - ASYNC-01 (run_coroutine_threadsafe bridge — phase 1)
  provides:
    - PERF-04 (per-path 2-second debounce on file events)
  affects:
    - aws_copier/core/folder_watcher.py
    - tests/unit/test_folder_watcher.py
tech_stack:
  added: []
  patterns:
    - "asyncio.create_task + asyncio.sleep(2) per-path debounce dict pattern"
    - "CancelledError-silencing inside debounced coroutine"
    - "Pitfall 2 guard: cancel_all_pending called before observer.stop()"
key_files:
  created: []
  modified:
    - aws_copier/core/folder_watcher.py
    - tests/unit/test_folder_watcher.py
decisions:
  - "2-second debounce window (D-06): fixed per plan — collapses atomic-save editor bursts (Vim, Emacs, IDE temp-rename patterns)"
  - "Per-path keying via str(file_path): distinct file paths each have independent timers, not a global gate"
  - "Dict access from asyncio loop thread only (via run_coroutine_threadsafe): no additional locking needed"
  - "cancel_all_pending is sync (not async): allows calling from FolderWatcher.stop without await chaining issues"
  - "Three existing tests updated to patch _schedule_debounced instead of _process_changed_file: they test on_any_event wiring, not the debounce internals"
metrics:
  duration: "~12 minutes"
  completed: "2026-04-26T05:41:54Z"
  tasks_completed: 1
  files_modified: 2
---

# Phase 2 Plan 04: PERF-04 Per-Path Event Debounce Summary

Implemented 2-second per-path debounce on file system events in `FileChangeHandler`. Rapid successive events for the same file path (atomic-save editor patterns) collapse to a single `_process_changed_file` invocation, eliminating redundant folder scans.

## New Methods Added to FileChangeHandler

| Method | Signature | Summary |
|--------|-----------|---------|
| `_schedule_debounced` | `async def _schedule_debounced(self, file_path: Path, event_type: str) -> None` | Cancels any pending debounce task for this path, then creates a fresh `asyncio.Task` with a 2-second timer. Dict is accessed only from the asyncio loop thread (safe, no lock needed). |
| `_debounced_process` | `async def _debounced_process(self, file_path: Path, event_type: str) -> None` | Sleeps 2 seconds then delegates to `_process_changed_file`. Catches and silences `asyncio.CancelledError` — cancellation means a newer event superseded this one, which is normal. |
| `cancel_all_pending` | `def cancel_all_pending(self) -> None` | Iterates `_debounce_tasks`, cancels every non-done task, then clears the dict. Called from `FolderWatcher.stop()` before `observer.stop()` to prevent tasks firing after event loop shutdown (Pitfall 2 guard). |

New instance attribute: `self._debounce_tasks: Dict[str, asyncio.Task] = {}` — added in `FileChangeHandler.__init__` at line 46.

## Modification Points

**`on_any_event` change (line 74-77):** `asyncio.run_coroutine_threadsafe` now schedules `self._schedule_debounced(file_path, event.event_type)` instead of `self._process_changed_file(file_path, event.event_type)` directly. The surrounding `try/except Exception` block is unchanged.

**`FolderWatcher.stop` change (lines 217-220):** Added a loop over `self.handlers.values()` calling `handler.cancel_all_pending()` immediately after the "Stopping folder watcher" log and before `self.observer.stop()`. This ensures pending debounce tasks are cancelled cleanly before the observer stops and the event loop begins shutdown.

## New Tests in TestEventDebounce

5 tests added, all passing:

| Test | What It Proves |
|------|---------------|
| `test_rapid_events_collapse_to_one_call` | 3 rapid `_schedule_debounced` calls for the same path produce exactly 1 `_process_current_folder` call after 2.3s |
| `test_distinct_paths_debounce_independently` | 2 events for 2 different paths each create independent pending tasks; 2 calls after 2.3s |
| `test_new_event_cancels_previous` | Second schedule for the same path cancels the first task and replaces it in the dict |
| `test_cancelled_error_silenced` | Manual `cancel_all_pending()` before sleep elapses produces no ERROR-level log entries |
| `test_on_any_event_schedules_debounced_wrapper` | `on_any_event` passes a coroutine whose `cr_code.co_name == "_schedule_debounced"` to `run_coroutine_threadsafe` |

**Runtime sensitivity note:** `test_rapid_events_collapse_to_one_call` and `test_distinct_paths_debounce_independently` use `await asyncio.sleep(2.3)` — a real 2.3-second wall-clock wait. On slow CI machines or under heavy load these tests may be slow but are not flaky in terms of correctness (the 2.3s > 2s window provides 300ms headroom).

## Existing Tests Updated (No Test Made Obsolete)

Three pre-existing tests in `TestFileChangeHandler` that tested `on_any_event` wiring were updated to patch `_schedule_debounced` instead of `_process_changed_file`:

1. `test_on_any_event_uses_run_coroutine_threadsafe` — patching updated; assertion on loop arg preserved
2. `test_on_any_event_does_not_use_call_soon_threadsafe` — patching updated; assertion on `call_soon_threadsafe` count preserved
3. `test_on_any_event_file_modified` — patching updated; assertion on `run_coroutine_threadsafe` call count preserved

All 29 pre-existing tests continue to pass. Total suite: 149 passed.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated existing tests to patch _schedule_debounced instead of _process_changed_file**
- **Found during:** GREEN phase test run
- **Issue:** Three existing tests patched `_process_changed_file` on `on_any_event` tests, but `on_any_event` now calls `_schedule_debounced`. This caused `RuntimeWarning: coroutine 'FileChangeHandler._schedule_debounced' was never awaited` (real coroutine leaked).
- **Fix:** Changed `patch.object(handler, "_process_changed_file")` to `patch.object(handler, "_schedule_debounced")` in the three affected tests. The intent of each test (verifying `run_coroutine_threadsafe` is called / `call_soon_threadsafe` is not called) is unchanged.
- **Files modified:** `tests/unit/test_folder_watcher.py`
- **Commit:** ea08b5f (included in GREEN commit)

## Known Stubs

None — all new methods are fully implemented.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced. The `_debounce_tasks` dict is bounded by distinct file paths within watched folders (single-user tool; T-02-18 accepted in plan threat model). All threat mitigations from the plan's threat register are implemented:

- T-02-19 (race condition): mitigated — dict accessed only from asyncio loop thread
- T-02-20 (info disclosure): mitigated — task name uses `file_path.name` (basename only)
- T-02-21 (Pitfall 2 post-shutdown): mitigated — `cancel_all_pending` called in `FolderWatcher.stop`

## TDD Gate Compliance

- RED gate: commit `e088082` — `test(02-04): add failing tests for PERF-04 per-path debounce`
- GREEN gate: commit `ea08b5f` — `feat(02-04): implement PERF-04 per-path 2-second debounce on file events`
- REFACTOR gate: not needed — implementation was clean on first pass

## Self-Check: PASSED

- `aws_copier/core/folder_watcher.py` — FOUND (modified)
- `tests/unit/test_folder_watcher.py` — FOUND (modified)
- Commit `e088082` — FOUND (RED gate)
- Commit `ea08b5f` — FOUND (GREEN gate)
- `uv run pytest tests/unit/test_folder_watcher.py::TestEventDebounce` — 5 passed
- `uv run pytest tests/unit/test_folder_watcher.py` — 34 passed
- `uv run pytest -x` — 149 passed
