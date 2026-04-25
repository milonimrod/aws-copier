---
phase: 01-core-reliability
verified: 2026-04-25T00:30:00Z
status: passed
score: 5/5
overrides_applied: 0
gaps: []
---

# Phase 1: Core Reliability — Verification Report

**Phase Goal:** The daemon correctly syncs files in real time — real-time events land in the event loop, uploads run concurrently, ignore patterns work as globs, dot-files never reach S3, and SIGTERM triggers a clean drain instead of a mid-upload kill

**Verified:** 2026-04-25T00:30:00Z
**Status:** passed
**Re-verification:** Yes — ASYNC-04 gap fixed (s3_manager.py:204 updated to get_running_loop)

---

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A file saved in a watched folder while the daemon is running triggers an S3 upload without requiring the next 5-minute scan cycle | VERIFIED | `folder_watcher.py:67` uses `asyncio.run_coroutine_threadsafe` (ASYNC-01). `FileChangeHandler.on_any_event` schedules `_process_changed_file` directly on the event loop. Old `call_soon_threadsafe(asyncio.create_task, coro)` is gone. |
| 2 | When 10 files change simultaneously, all 10 upload concurrently up to the configured semaphore limit, not one-at-a-time | VERIFIED | `file_listener.py:380` uses `asyncio.gather(*tasks, return_exceptions=True)` (ASYNC-02). Per-task 300s timeout wrapped inside `_upload_with_timeout` coroutine so windows run concurrently. `upload_semaphore = asyncio.Semaphore(self.config.max_concurrent_uploads)` (CONFIG-01). |
| 3 | A file named `report.bak` or `script.pyc` in a watched folder is never uploaded to S3 | VERIFIED | `ignore_rules.py` `GLOB_PATTERNS` contains `*.bak`, `*.pyc`. `IGNORE_RULES.should_ignore_file` uses `fnmatch.fnmatch` (IGNORE-01). Consumed via `IGNORE_RULES.should_ignore_file` in both `file_listener.py:214` and `folder_watcher.py:57` (IGNORE-03). 24 unit tests pass confirming this. |
| 4 | A `.env` file or SSH key (`id_rsa`, `*.pem`) in a watched folder is never uploaded to S3 | VERIFIED | `ignore_rules.py` `SENSITIVE_DENY` contains `.env`, `.env.*`, `*.pem`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519` (IGNORE-02). Dot-prefix guard (`name.startswith(".")`) catches all dotfiles before the explicit list. Tests in `TestIgnoreRulesSensitiveDeny` confirm. |
| 5 | Sending SIGTERM to the headless daemon waits for in-flight uploads to finish before exiting, and `uv run aws-copier` launches the daemon without error | VERIFIED | `main.py:42` calls `self._setup_signal_handlers()` (uncommented). Unix: `loop.add_signal_handler` (line 129). Windows: `signal.signal` + `loop.call_soon_threadsafe` fallback (lines 137-144). Drain: `asyncio.wait(upload_tasks, timeout=60)` (line 97). `pyproject.toml:30` has `aws-copier = "main:sync_main"` (CONFIG-02). 8 signal-handling unit tests pass. |

**Score:** 5/5 roadmap success criteria verified

---

### Derived Must-Haves from PLAN Frontmatter

All PLAN must_haves from Plans 01-05 were verified against the codebase. Summary of each plan:

**Plan 01 (IGNORE-01/02/03) — all truths verified:**
- `ignore_rules.py` exists with `@dataclass(frozen=True)` `IgnoreRules` class and `IGNORE_RULES` singleton
- `fnmatch.fnmatch` used for glob patterns
- `should_ignore_file` + `should_ignore_dir` methods functional
- `FrozenInstanceError` on mutation attempt
- `tests/unit/test_ignore_rules.py`: 24 tests, all pass

**Plan 02 (CONFIG-02/03/04) — all truths verified:**
- `pyproject.toml:30`: `aws-copier = "main:sync_main"` (corrected)
- `ruff` and `python-dotenv` absent from `[project].dependencies`
- Both present in `[dependency-groups].dev`
- `SimpleConfig` has no `discovered_files_folder` field or `create_directories` method
- `tests/unit/test_simple_config.py`: includes `test_discovered_files_folder_removed` and `test_legacy_config_with_discovered_files_folder_ignored`

**Plan 03 (ASYNC-01/05, IGNORE-03) — all truths verified:**
- `asyncio.run_coroutine_threadsafe` at `folder_watcher.py:67`
- `asyncio.AbstractEventLoop` annotation at `folder_watcher.py:27`
- `IGNORE_RULES` imported and used; no `self.ignore_patterns` or `_should_ignore_file`

**Plan 04 (ASYNC-02/03/04, CONFIG-01, IGNORE-03/04) — all truths verified (ASYNC-04 gap fixed):**
- `asyncio.gather(*tasks, return_exceptions=True)` at `file_listener.py:380`
- `aiofiles.open` at lines 188, 435 under per-folder `asyncio.Lock`
- `upload_semaphore = asyncio.Semaphore(self.config.max_concurrent_uploads)` at line 35
- `self._active_upload_tasks: Set[asyncio.Task]` at line 46
- `IGNORE_RULES.should_ignore_file` / `IGNORE_RULES.should_ignore_dir` used; old methods gone
- `_stats["ignored_files"]` incremented at `file_listener.py:215`
- **ASYNC-04 gap:** `s3_manager.py:204` retains `asyncio.get_event_loop()` — see Gaps section

**Plan 05 (ASYNC-06) — all truths verified:**
- `_setup_signal_handlers()` called at `main.py:42` (uncommented)
- `loop.add_signal_handler` for Unix at `main.py:129`
- Windows fallback at `main.py:136-144`
- `asyncio.wait(upload_tasks, timeout=60)` at `main.py:97`
- `logger.warning(f"Abandoned in-flight upload: {task.get_name()}")` at `main.py:101`
- `self.file_listener._active_upload_tasks` referenced at `main.py:94`

---

## Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `aws_copier/core/ignore_rules.py` | IgnoreRules frozen dataclass + IGNORE_RULES singleton | VERIFIED | 149 lines, `@dataclass(frozen=True)`, `IGNORE_RULES: IgnoreRules = IgnoreRules()`, `fnmatch` used |
| `tests/unit/test_ignore_rules.py` | 20+ behavior-proving tests | VERIFIED | 24 tests in 5 test classes, all pass |
| `pyproject.toml` | Corrected entrypoint + cleaned deps | VERIFIED | `aws-copier = "main:sync_main"`, ruff/python-dotenv absent from runtime |
| `aws_copier/models/simple_config.py` | No `discovered_files_folder` or `create_directories` | VERIFIED | 0 occurrences of either in the file |
| `tests/unit/test_simple_config.py` | Tests for removed field | VERIFIED | Two new regression tests added |
| `aws_copier/core/folder_watcher.py` | run_coroutine_threadsafe + AbstractEventLoop + IGNORE_RULES | VERIFIED | All three present; old patterns absent |
| `aws_copier/core/file_listener.py` | gather, aiofiles, config-wired semaphore, IGNORE_RULES, active tasks | VERIFIED | All six fixes present; no banned patterns |
| `main.py` | Signal handling re-enabled + 60s drain | VERIFIED | `_setup_signal_handlers()` uncommented; drain logic present |
| `tests/unit/test_signal_handling.py` | 100+ line signal test file | VERIFIED | 171 lines, 8 tests, all pass |

---

## Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `FileChangeHandler.on_any_event` | asyncio event loop | `asyncio.run_coroutine_threadsafe` | WIRED | `folder_watcher.py:67-70` |
| `folder_watcher.py` | `aws_copier.core.ignore_rules.IGNORE_RULES` | `IGNORE_RULES.should_ignore_file` | WIRED | `folder_watcher.py:12` import + `line:57` usage |
| `FileListener._upload_files` | `asyncio.gather` | `await asyncio.gather(*tasks, return_exceptions=True)` | WIRED | `file_listener.py:380` |
| `FileListener._load_backup_info` / `_update_backup_info` | `aiofiles.open` | `async with aiofiles.open(...)` | WIRED | `file_listener.py:188, 435` |
| `FileListener.__init__` | `self.config.max_concurrent_uploads` | `asyncio.Semaphore(self.config.max_concurrent_uploads)` | WIRED | `file_listener.py:35` |
| `FileListener` scan path | `IGNORE_RULES` | `IGNORE_RULES.should_ignore_file` / `should_ignore_dir` | WIRED | `file_listener.py:100, 112, 214` |
| `AWSCopierApp._setup_signal_handlers` (Unix) | asyncio event loop | `loop.add_signal_handler(sig, ...)` | WIRED | `main.py:129` |
| `AWSCopierApp.shutdown` | `FileListener._active_upload_tasks` | `asyncio.wait(self.file_listener._active_upload_tasks, timeout=60)` | WIRED | `main.py:94-97` |
| drain timeout branch | `logger.warning` for abandoned file | `logger.warning(f"Abandoned in-flight upload: {task.get_name()}")` | WIRED | `main.py:101` |
| `pyproject.toml [project.scripts]` | `main.py:sync_main` | `aws-copier = "main:sync_main"` | WIRED | `pyproject.toml:30` |

---

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| IGNORE_RULES functional: bak/pyc/env/id_rsa blocked, normal files pass | `uv run python -c "from aws_copier.core.ignore_rules import IGNORE_RULES ..."` | All assertions pass | PASS |
| SimpleConfig has no discovered_files_folder | `uv run python -c "from aws_copier.models.simple_config import SimpleConfig; c = SimpleConfig(); assert not hasattr(c, 'discovered_files_folder')"` | Exit 0 | PASS |
| FileListener: gather, aiofiles, _active_upload_tasks, IGNORE_RULES all present | `uv run python -c "from aws_copier.core.file_listener import FileListener; import inspect; src = inspect.getsource(FileListener); assert 'asyncio.gather' in src ..."` | Exit 0 | PASS |
| FileChangeHandler: run_coroutine_threadsafe, no call_soon_threadsafe, no _should_ignore_file | `uv run python -c "from aws_copier.core.folder_watcher import FileChangeHandler; ..."` | Exit 0 | PASS |
| main.py: loop.add_signal_handler, asyncio.wait timeout=60, drain reference, _setup called | `uv run python -c "import main; import inspect; src = inspect.getsource(main.AWSCopierApp); ..."` | Exit 0 | PASS |
| Full test suite | `uv run pytest -q --no-cov` | 144 passed, 3 warnings | PASS |

---

## Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| ASYNC-01 | Plan 03 | `run_coroutine_threadsafe` bridge | SATISFIED | `folder_watcher.py:67` |
| ASYNC-02 | Plan 04 | `asyncio.gather` concurrent uploads | SATISFIED | `file_listener.py:380` |
| ASYNC-03 | Plan 04 | `aiofiles` + per-folder `asyncio.Lock` | SATISFIED | `file_listener.py:187-193, 434-436` |
| ASYNC-04 | Plan 04 | `get_event_loop()` replaced throughout | SATISFIED | `file_listener.py` + `s3_manager.py:204` both use `get_running_loop()` (gap fixed post-initial-verification) |
| ASYNC-05 | Plan 03 | `AbstractEventLoop` annotation | SATISFIED | `folder_watcher.py:27` |
| ASYNC-06 | Plan 05 | Signal handling + 60s drain | SATISFIED | `main.py:42, 115-145, 77-113` |
| IGNORE-01 | Plan 01 | `fnmatch.fnmatch` for glob patterns | SATISFIED | `ignore_rules.py:115, 119` |
| IGNORE-02 | Plan 01 | Sensitive/dot-file deny list | SATISFIED | `ignore_rules.py:111, 114-116` |
| IGNORE-03 | Plans 01/03/04 | Shared `IgnoreRules` singleton | SATISFIED | All three consumers wired |
| IGNORE-04 | Plan 04 | `_stats["ignored_files"]` increments | SATISFIED | `file_listener.py:215` |
| CONFIG-01 | Plan 04 | `upload_semaphore` wired to config | SATISFIED | `file_listener.py:35` |
| CONFIG-02 | Plan 02 | Corrected `aws-copier` entrypoint | SATISFIED | `pyproject.toml:30` |
| CONFIG-03 | Plan 02 | `discovered_files_folder` removed | SATISFIED | 0 occurrences in `simple_config.py` |
| CONFIG-04 | Plan 02 | `ruff`/`python-dotenv` moved to dev | SATISFIED | `[dependency-groups].dev` lines 90-91 |

---

## Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `aws_copier/core/s3_manager.py` | 204 | `asyncio.get_event_loop()` in async method | Warning | Emits `DeprecationWarning` on Python 3.10+ when called outside a running loop; harmless in async context but violates ASYNC-04 requirement |

No placeholder content, TODO stubs, empty implementations, or data-flow disconnects found in any files modified by Phase 1.

---

## Gaps Summary

No gaps. All 14 requirements satisfied. ASYNC-04 gap found in initial verification (s3_manager.py:204) was fixed post-verification — `asyncio.get_event_loop()` replaced with `asyncio.get_running_loop()`.

---

_Verified: 2026-04-24T23:58:27Z_
_Verifier: Claude (gsd-verifier)_
