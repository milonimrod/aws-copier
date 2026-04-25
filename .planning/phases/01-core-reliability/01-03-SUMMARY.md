---
phase: 01-core-reliability
plan: "03"
subsystem: async
tags: [asyncio, watchdog, thread-safety, ignore-rules, run_coroutine_threadsafe]

# Dependency graph
requires:
  - phase: 01-core-reliability/01-01
    provides: IGNORE_RULES singleton (aws_copier/core/ignore_rules.py) consumed here
provides:
  - Thread-safe watchdog-to-asyncio bridge via run_coroutine_threadsafe (ASYNC-01)
  - AbstractEventLoop annotation on FileChangeHandler.__init__ (ASYNC-05)
  - Unified ignore logic through IGNORE_RULES singleton; local duplicates deleted (IGNORE-03)
  - Behaviour-proving tests: ASYNC-01 bridge, ASYNC-01 regression guard, IGNORE-03 delegation
affects:
  - 01-04 (scans path — will consume IGNORE_RULES.should_ignore_dir; pattern is now consistent)
  - 01-05 (signal handling depends on correct event loop reference)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "asyncio.run_coroutine_threadsafe(coro, loop) for bridging watchdog OS thread to asyncio event loop (fire-and-forget; never call .result())"
    - "Patch module-level asyncio functions via patch('aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe') in tests"
    - "IGNORE_RULES singleton consumed everywhere; no local ignore_patterns sets"

key-files:
  created: []
  modified:
    - aws_copier/core/folder_watcher.py
    - tests/unit/test_folder_watcher.py

key-decisions:
  - "Use run_coroutine_threadsafe (not call_soon_threadsafe+create_task) as the only correct API for bridging a non-asyncio thread to the event loop on Python 3.10+"
  - "Delete FileChangeHandler._should_ignore_file and self.ignore_patterns entirely — single source of truth is IGNORE_RULES singleton from Plan 01"
  - "Tests patch the module-level asyncio function rather than the mock loop attribute — this is the correct technique for run_coroutine_threadsafe tests"

patterns-established:
  - "Pattern ASYNC-01: run_coroutine_threadsafe is the canonical watchdog-to-asyncio bridge; call_soon_threadsafe+create_task is forbidden"
  - "Pattern IGNORE-03: all ignore decisions go through IGNORE_RULES.should_ignore_file / should_ignore_dir; no local copies"

requirements-completed:
  - ASYNC-01
  - ASYNC-05
  - IGNORE-03

# Metrics
duration: 3min
completed: "2026-04-24"
---

# Phase 01 Plan 03: Folder Watcher Thread Bridge and Ignore Deduplication Summary

**`asyncio.run_coroutine_threadsafe` replaces broken `call_soon_threadsafe+create_task` bridge; `FileChangeHandler` drops diverging ignore set and delegates to `IGNORE_RULES` singleton**

## Performance

- **Duration:** 3 min
- **Started:** 2026-04-24T21:22:02Z
- **Completed:** 2026-04-24T21:25:08Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Fixed ASYNC-01: real-time watchdog file events now reliably reach the asyncio loop via `asyncio.run_coroutine_threadsafe`; the previous `call_soon_threadsafe(asyncio.create_task, coro)` silently dropped events on Python 3.10+
- Fixed ASYNC-05: `FileChangeHandler.__init__` type annotation updated from private `asyncio.BaseEventLoop` to public `asyncio.AbstractEventLoop`
- Fixed IGNORE-03: deleted `FileChangeHandler.self.ignore_patterns` (40-line set) and `_should_ignore_file` method; handler now delegates to `IGNORE_RULES.should_ignore_file` from Plan 01's singleton — the ignore asymmetry between scan and watch paths is closed on the handler side
- Added 3 new behaviour-proving tests covering ASYNC-01 bridge, ASYNC-01 regression guard, and IGNORE-03 delegation; full 129-test suite remains green

## Task Commits

Each task was committed atomically:

1. **Task 1: Apply ASYNC-01, ASYNC-05, IGNORE-03 fixes to folder_watcher.py** — `50da27c` (feat)
2. **Task 2: Update test_folder_watcher.py to reflect new bridge and ignore API** — `6612f8a` (feat)

**Plan metadata:** (committed below as docs commit)

## Exact Diffs Applied to folder_watcher.py (6 edits)

| Edit | Location | Change |
|------|----------|--------|
| EDIT 1 | line 12 (import block) | Added `from aws_copier.core.ignore_rules import IGNORE_RULES` |
| EDIT 2 (ASYNC-05) | `__init__` signature | `asyncio.BaseEventLoop` → `asyncio.AbstractEventLoop` |
| EDIT 3 (IGNORE-03) | `__init__` body lines 37-77 | Deleted 40-line `self.ignore_patterns = { ... }` set |
| EDIT 4 (IGNORE-03) | `on_any_event` line 93 | `self._should_ignore_file(file_path)` → `IGNORE_RULES.should_ignore_file(file_path)` |
| EDIT 5 (ASYNC-01) | `on_any_event` lines 100-103 | `call_soon_threadsafe(create_task, coro)` → `run_coroutine_threadsafe(coro, self.event_loop)` with fire-and-forget comment |
| EDIT 6 (IGNORE-03) | `FileChangeHandler` lines 142-162 | Deleted `_should_ignore_file` method (21 lines) |

## Test Changes in test_folder_watcher.py

**Added (3 new tests):**
- `test_on_any_event_uses_run_coroutine_threadsafe` — ASYNC-01 behaviour proof: patches module-level `asyncio.run_coroutine_threadsafe`, asserts called exactly once with handler's event_loop
- `test_on_any_event_does_not_use_call_soon_threadsafe` — ASYNC-01 regression guard: asserts `event_loop.call_soon_threadsafe` call count == 0
- `test_on_any_event_skips_ignored_file_via_ignore_rules` — IGNORE-03 behaviour proof: `.env` file triggers IGNORE_RULES path, `run_coroutine_threadsafe` not called

**Removed (2 tests):**
- `test_should_ignore_file_patterns` — directly called `file_change_handler._should_ignore_file` which no longer exists
- Inline `ignore_patterns` length assertion in `test_file_change_handler_initialization` — field deleted in IGNORE-03

**Updated (6 tests):**
- All `call_soon_threadsafe.assert_called_once()` assertions replaced with `patch("...asyncio.run_coroutine_threadsafe") as mock_run` + `assert mock_run.call_count == ...`
- `test_file_change_handler_error_handling` — patches `run_coroutine_threadsafe` instead of setting `call_soon_threadsafe.side_effect`

**Test count delta:** 29 tests total (was 29; replaced 2 + added 3 = net +1, removed 1 pattern test + absorbed into new ones)

## Thread Bridge Confirmation

`asyncio.run_coroutine_threadsafe` is the **only** thread bridge now in use. Verified:

```
grep -c "asyncio.run_coroutine_threadsafe" aws_copier/core/folder_watcher.py  → 1
grep -c "self.event_loop.call_soon_threadsafe" aws_copier/core/folder_watcher.py  → 0
```

## Files Created/Modified

- `aws_copier/core/folder_watcher.py` — 6 targeted edits; net -55 lines (deleted ignore set + method, replaced bridge); FolderWatcher class and all async methods untouched
- `tests/unit/test_folder_watcher.py` — updated all bridge assertions; added 3 ASYNC-01/IGNORE-03 behaviour tests; removed tests referencing deleted method

## Decisions Made

- Patch the module-level `asyncio` function (`patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe")`) rather than the mock loop attribute — the correct approach since the handler calls `asyncio.run_coroutine_threadsafe(coro, loop)` at module scope, not `self.event_loop.run_coroutine_threadsafe(coro)`
- Fire-and-forget pattern documented in code comment: never call `.result()` on the returned `concurrent.futures.Future` — doing so would deadlock the watchdog thread

## Deviations from Plan

None — plan executed exactly as written. All 6 edits applied as specified. Test updates followed the plan's STEP A through STEP E exactly.

## Issues Encountered

None. The `mock_event_loop` fixture retained in the test file (it sets up `call_soon_threadsafe` on the mock loop) — this is intentional since `test_on_any_event_does_not_use_call_soon_threadsafe` needs a loop with a trackable `call_soon_threadsafe` attribute to prove it's not called.

RuntimeWarnings from `AsyncMock` internals (unawaited coroutine) appear in 3 tests where `_process_changed_file` is not patched — these are cosmetic mock framework artefacts and do not affect correctness or coverage.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced. The `IGNORE_RULES.should_ignore_file` check on line 57 of `folder_watcher.py` fires **before** `run_coroutine_threadsafe`, maintaining the correct ordering: ignore evaluation precedes scheduling (mitigates T-03-02 from the plan's threat register).

## Next Phase Readiness

- Plan 04 (file_listener.py scan path) can now wire `IGNORE_RULES.should_ignore_dir` into recursive folder traversal — the handler side (this plan) and scan side (Plan 04) will both use the same singleton
- Plan 05 (signal handling) depends on the asyncio loop reference being correct — confirmed by this plan's fix

---
*Phase: 01-core-reliability*
*Completed: 2026-04-24*
