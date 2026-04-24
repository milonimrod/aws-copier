---
phase: 01-core-reliability
plan: "05"
subsystem: main
tags: [signal-handling, graceful-shutdown, asyncio, drain, platform-compat]
dependency_graph:
  requires: ["01-04"]
  provides: ["ASYNC-06"]
  affects: ["main.py"]
tech_stack:
  added: []
  patterns:
    - "loop.add_signal_handler for Unix signal registration on running asyncio loop"
    - "signal.signal + loop.call_soon_threadsafe for Windows fallback"
    - "asyncio.wait with timeout=60 for bounded drain of in-flight upload tasks"
    - "Idempotency guard (if not self.running: return) prevents double-drain on re-entrant shutdown"
key_files:
  created:
    - tests/unit/test_signal_handling.py
  modified:
    - main.py
decisions:
  - "Used sys.platform != 'win32' (RESEARCH.md Pattern 5) over legacy os.name == 'nt'"
  - "Placed _setup_signal_handlers() call AFTER S3Manager.initialize() so asyncio.get_running_loop() is valid"
  - "Drain reads FileListener._active_upload_tasks directly (no new method on FileListener) per interface spec"
  - "Deleted _set_shutdown_event helper — superseded by _handle_signal; no other callers"
  - "Removed now-unused os import after replacing os.name check with sys.platform"
metrics:
  duration: "~15 minutes"
  completed: "2026-04-24T21:33:20Z"
  tasks_completed: 2
  files_modified: 1
  files_created: 1
  tests_added: 8
  tests_total: 144
---

# Phase 1 Plan 05: Signal Handling + Graceful Drain Summary

**One-liner:** Platform-aware SIGTERM/SIGINT handlers registered on the running asyncio loop with a 60-second drain of `FileListener._active_upload_tasks` before process exit.

## What Was Built

### main.py — 5 targeted edits

**EDIT 1 — Removed unused `os` import:** After replacing `os.name == "nt"` with `sys.platform == "win32"`, `os` became unused and was removed (ruff F401 fix).

**EDIT 2 — Uncommented and relocated `_setup_signal_handlers()` call:** Moved from after `scan_all_folders` to immediately after `await self.s3_manager.initialize()`. The call is now live (not commented out) so handlers are registered before the initial scan begins, ensuring SIGTERM during startup also triggers a graceful drain.

**EDIT 3 — Rewrote `_setup_signal_handlers`:** The broken `signal.signal + asyncio.create_task` approach (which had no running-loop reference) was replaced with:
- **Unix** (`sys.platform != "win32"`): `loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(self._handle_signal(s)))` — delivers signals directly to the running event loop. Lambda default-arg `s=sig` pins the signal value correctly across loop iterations (T-05-03 mitigation).
- **Windows** (`sys.platform == "win32"`): `signal.signal` with a synchronous `_win_handler` that does only `loop.call_soon_threadsafe(asyncio.ensure_future, self._handle_signal(signum))` — constant-time scheduling, no blocking (T-05-06 mitigation).

**EDIT 4 — Added `_handle_signal` async method:** Replaces the old inline `signal_handler` function. Sets `self.running = False` and `self.shutdown_event.set()` to wake the main status loop.

**EDIT 5 — Extended `shutdown()` with 60-second drain block:**
```
Step 1: folder_watcher.stop()        # stop new events feeding new tasks
Step 2: asyncio.wait(upload_tasks, timeout=60)  # drain in-flight uploads
         → log warning per abandoned task + task.cancel()
Step 3: s3_manager.close()           # release S3 client
```
The Pitfall 3 guard (`if upload_tasks:`) prevents `asyncio.wait` from being called on an empty set (would raise `ValueError`). The idempotency guard (`if not self.running: return`) prevents the drain running twice when both the signal handler and the `finally` clause call `shutdown()`.

**Deleted `_set_shutdown_event`:** The old async helper was superseded by `_handle_signal`. No other callers existed.

### tests/unit/test_signal_handling.py — 8 behaviour-proving tests

| Test | Class | What it proves |
|------|-------|----------------|
| `test_handle_signal_sets_running_false_and_event` | `TestHandleSignal` | `_handle_signal` flips `running=False` and sets `shutdown_event` |
| `test_signal_handlers_registered_on_unix` | `TestSetupSignalHandlers` | `loop.add_signal_handler` called twice (SIGTERM + SIGINT) on Unix |
| `test_signal_handlers_registered_on_windows` | `TestSetupSignalHandlers` | `signal.signal` used when `sys.platform == "win32"` |
| `test_drain_waits_for_fast_uploads` | `TestShutdownDrain` | 3 fast tasks (50ms) all complete; no `Abandoned` warning logged |
| `test_drain_times_out_and_warns_and_cancels` | `TestShutdownDrain` | `asyncio.wait` mock returns immediately as timed-out; warning contains task name; task cancelled |
| `test_drain_skips_when_no_active_uploads` | `TestShutdownDrain` | Empty task set → `asyncio.wait` never called (Pitfall 3 guard) |
| `test_shutdown_calls_folder_watcher_stop_and_s3_close` | `TestShutdownDrain` | Existing shutdown sequence (stop watcher, close S3) preserved |
| `test_shutdown_is_idempotent` | `TestShutdownDrain` | Second `shutdown()` call returns early; `folder_watcher.stop` called only once |

## Key Acceptance Criteria (all passing)

```
grep -c "loop.add_signal_handler" main.py        → 4 (1 functional + 3 in docstring/comments)
grep -c "asyncio.wait(upload_tasks, timeout=60)" main.py  → 1
grep -c "Abandoned in-flight upload" main.py     → 1
grep -c "self._setup_signal_handlers()" main.py  → 1 (live, not commented)
grep -c "# self._setup_signal_handlers()" main.py → 0
grep -c "async def _handle_signal" main.py       → 1
grep -c "async def _set_shutdown_event" main.py  → 0 (deleted)
grep -c 'sys.platform != "win32"' main.py        → 1
grep -c "self.file_listener._active_upload_tasks" main.py → 1
uv run python -c "import main"                   → exit 0
uv run ruff check main.py                        → exit 0
uv run pytest tests/unit/test_signal_handling.py → 8 passed
uv run pytest --no-cov -q                        → 144 passed
```

## Threat Mitigations Applied

| Threat | Mitigation | Test |
|--------|-----------|------|
| T-05-01: Mid-upload SIGTERM kills daemon | 60s drain window | `test_drain_waits_for_fast_uploads` |
| T-05-02: No log of abandoned files | `logger.warning(f"Abandoned in-flight upload: {task.get_name()}")` | `test_drain_times_out_and_warns_and_cancels` |
| T-05-03: Coroutine passed directly to `add_signal_handler` never fires | Lambda callable + `asyncio.ensure_future` | `test_signal_handlers_registered_on_unix` |
| T-05-04: `asyncio.wait(empty_set)` raises ValueError | `if upload_tasks:` guard | `test_drain_skips_when_no_active_uploads` |
| T-05-06: Windows signal handler blocks loop | Handler body does only `call_soon_threadsafe` | `test_signal_handlers_registered_on_windows` |
| T-05-08: Re-entrant shutdown runs drain twice | `if not self.running: return` idempotency | `test_shutdown_is_idempotent` |

## Phase 1 Completion

This is the final plan in Phase 1. All 5 plans have been executed:

| Plan | Requirement | Summary |
|------|------------|---------|
| 01-01 | IGNORE-01, IGNORE-02, IGNORE-03, IGNORE-04 | `ignore_rules.py` singleton with fnmatch glob matching and hidden-file blocking |
| 01-02 | CONFIG-02, CONFIG-03, CONFIG-04 | Fixed CLI entrypoint, `max_concurrent_uploads` wiring, `ignored_files` stat counter |
| 01-03 | ASYNC-01, ASYNC-04, ASYNC-05 | Thread bridge replaced with `run_coroutine_threadsafe`; ignore patterns deduplicated |
| 01-04 | ASYNC-02, ASYNC-03, CONFIG-01 | `asyncio.gather` concurrent uploads, `aiofiles` async I/O, per-folder lock |
| 01-05 | ASYNC-06 | Platform-aware signal handlers + 60s drain (this plan) |

## Commits

| Hash | Description |
|------|-------------|
| `d07010a` | `feat(01-05): rewrite signal handlers with 60s drain and platform dispatch` |
| `1c23f6d` | `test(01-05): add signal handling behaviour tests for ASYNC-06` |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Removed unused `os` import**
- **Found during:** Task 1, ruff check after EDIT 3
- **Issue:** After replacing `os.name == "nt"` with `sys.platform == "win32"`, `os` was imported but unused (ruff F401)
- **Fix:** Removed `import os` from the import block; sorted remaining imports alphabetically per ruff convention
- **Files modified:** `main.py`
- **Commit:** `d07010a`

**2. [Rule 1 - Bug] Removed unused `pathlib.Path` import from test file**
- **Found during:** Task 2, ruff check after writing test file
- **Issue:** `from pathlib import Path` was included in the test file template but never used in the actual test code
- **Fix:** Removed the unused import
- **Files modified:** `tests/unit/test_signal_handling.py`
- **Commit:** `1c23f6d`

## Known Stubs

None — all signal handling logic is fully wired. The drain reads `FileListener._active_upload_tasks` which is populated by Plan 04's `_upload_files` method.

## Self-Check: PASSED

- [x] `main.py` exists and contains all required patterns
- [x] `tests/unit/test_signal_handling.py` exists with 171 lines / 8 tests
- [x] Commit `d07010a` exists (feat - main.py)
- [x] Commit `1c23f6d` exists (test - test_signal_handling.py)
- [x] 144 tests pass across full suite
- [x] ruff check and format both clean
