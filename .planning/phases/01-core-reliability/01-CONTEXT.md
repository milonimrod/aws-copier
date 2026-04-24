# Phase 1: Core Reliability - Context

**Gathered:** 2026-04-24
**Status:** Ready for planning

<domain>
## Phase Boundary

Fix 14 correctness bugs across async event handling, ignore rules, and config cleanup so the daemon reliably syncs files in real time without leaking secrets or crashing on shutdown. All work is refactoring existing code — no new capabilities are added in this phase. Implementation order locked in STATE.md: ignore_rules.py first, then thread bridge, gather, aiofiles + per-folder lock, signal handling last.

</domain>

<decisions>
## Implementation Decisions

### Testing Strategy
- **D-01:** Each fix gets a corresponding behavior-proving test using the existing `pytest-asyncio` + `moto[s3]` setup. Tests must prove the bug is actually fixed (e.g., a real-time watchdog event triggers an S3 upload without a scan cycle; 10 simultaneous changed files upload concurrently; SIGTERM drains in-flight uploads before the process exits).
- **D-02:** Tests co-located with the fix work — no separate test phase.

### Shutdown Drain Behavior (ASYNC-06)
- **D-03:** On SIGTERM, drain in-flight uploads for up to **60 seconds**, then force-exit even if some uploads are still running.
- **D-04:** Each upload that doesn't complete within the drain window gets a `logger.warning()` naming the abandoned file, so the user knows what to expect on the next scan cycle.

### IgnoreRules Module Interface (IGNORE-03)
- **D-05:** `IgnoreRules` is a frozen dataclass in `aws_copier/core/ignore_rules.py` with **instance methods** `should_ignore_file(path: Path) -> bool` and `should_ignore_dir(path: Path) -> bool`. Logic is fully centralized — callers never re-implement the check.
- **D-06:** A module-level singleton `IGNORE_RULES = IgnoreRules()` is exported from `ignore_rules.py`. `FileListener` and `FileChangeHandler` both import and use this single instance — no per-component instantiation.

### Claude's Discretion
- Exact fnmatch pattern list for IGNORE-01 (glob patterns to expand from the current set)
- Exact dot-file/sensitive-file deny list for IGNORE-02 (SSH keys, `.pem`, `.key`, etc.)
- Internal structure of the per-folder `asyncio.Lock` registry in `FileListener` (ASYNC-03)
- Whether `asyncio.wait_for` wraps individual upload coroutines or gathered tasks (ASYNC-02)
- How to surface the drain countdown in logs during shutdown

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase Requirements
- `.planning/REQUIREMENTS.md` — all 14 v1 requirement specs with exact method signatures, module names, and implementation constraints (ASYNC-01 through CONFIG-04)

### Project Configuration
- `pyproject.toml` — current entrypoint definition, dependency sections, ruff/mypy config
- `aws_copier/core/file_listener.py` — primary file being refactored (18.7K, contains ignore rules, upload loop, backup state I/O, semaphore)
- `aws_copier/core/folder_watcher.py` — contains the broken `call_soon_threadsafe` bridge (line 101-102) and `FolderWatcher.start()`
- `main.py` — contains commented-out `_setup_signal_handlers()` (line 47) and `AWSCopierApp`
- `aws_copier/models/simple_config.py` — contains `discovered_files_folder` dead field (CONFIG-03)

### Test Infrastructure
- `tests/` — existing test files using `pytest-asyncio` (asyncio_mode = "auto") and `moto[s3]`

No external specs — requirements fully captured in decisions above and REQUIREMENTS.md.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `aiofiles` already imported in `file_listener.py` (used for MD5 in `_calculate_md5`) — extend to backup state I/O (ASYNC-03)
- `asyncio.Semaphore(50)` already exists as `upload_semaphore` — wire to `config.max_concurrent_uploads` (CONFIG-01)
- `asyncio.gather` already used for MD5 tasks (line 254) — apply same pattern to upload tasks (ASYNC-02)
- `moto[s3]` already in test deps — use for upload concurrency tests

### Established Patterns
- All async methods use `try/except Exception as e` + `logger.error()` — maintain in new code
- Private methods prefixed with `_` — `should_ignore_file`, `should_ignore_dir` become public methods on `IgnoreRules` (no underscore, they are the public API)
- Google-style docstrings with `Args:` and `Returns:` on all methods
- `asyncio.wait_for(..., timeout=300)` wraps individual file uploads — keep this pattern, per-task timeout

### Integration Points
- `FileChangeHandler.on_modified / on_created / on_deleted` → `event_loop.call_soon_threadsafe(...)` at `folder_watcher.py:101` — replace with `asyncio.run_coroutine_threadsafe`
- `FolderWatcher.start()` sets `self.event_loop = asyncio.get_running_loop()` (line 192) — this call is fine
- `AWSCopierApp.run()` in `main.py` — add `loop.add_signal_handler` after event loop is running (Unix), with `sys.platform == "win32"` guard

</code_context>

<specifics>
## Specific Ideas

No specific references — implementation is fully defined by REQUIREMENTS.md requirements and decisions above.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 01-core-reliability*
*Context gathered: 2026-04-24*
