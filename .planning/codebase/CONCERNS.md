# Codebase Concerns

**Analysis Date:** 2026-04-24

## Tech Debt

**Broken script entrypoint in pyproject.toml:**
- Issue: `pyproject.toml` declares `aws-copier = "simple_main:main"` but `simple_main.py` does not exist anywhere in the project. The actual entrypoints are `main.py` and `main_gui.py`.
- Files: `pyproject.toml` (line 32)
- Impact: `uv run aws-copier` and `pip install` + `aws-copier` CLI invocation will fail with a ModuleNotFoundError at runtime.
- Fix approach: Change the entrypoint to `main:sync_main` for the headless version or `main_gui:main` for the GUI version.

**`discovered_files_folder` config field is dead code:**
- Issue: `SimpleConfig` stores and serializes a `discovered_files_folder` path and `create_directories()` creates it, but nothing in the application ever reads files from it or writes to it.
- Files: `aws_copier/models/simple_config.py` (lines 42-45, 70, 81, 103)
- Impact: Unnecessary config field confuses users; the directory is created but never used.
- Fix approach: Remove the field and its serialization, or implement the intended functionality.

**Semaphore limits are hardcoded and disconnected from config:**
- Issue: `FileListener.__init__` creates `upload_semaphore` and `md5_semaphore` both hardcoded to 50. `SimpleConfig.max_concurrent_uploads` defaults to 100 and is stored, but `FileListener` never reads it. The config field is also in the YAML (`config.yaml` shows 100) but has no effect.
- Files: `aws_copier/core/file_listener.py` (lines 34, 37), `aws_copier/models/simple_config.py` (line 48)
- Impact: Users cannot tune concurrency through config; the config field gives false confidence.
- Fix approach: Initialize `upload_semaphore = asyncio.Semaphore(config.max_concurrent_uploads)` in `FileListener.__init__`.

**Signal handling is disabled in headless mode:**
- Issue: `AWSCopierApp._setup_signal_handlers()` is defined but the call is commented out (`# self._setup_signal_handlers()`) in `main.py` (line 47). The application has no graceful shutdown on SIGTERM/SIGINT in headless mode.
- Files: `main.py` (lines 46-47, 102-128)
- Impact: The process can only be killed forcefully; in-progress uploads may be corrupted or silently dropped on termination.
- Fix approach: Uncomment the signal handler call and use `loop.add_signal_handler` (the async-safe variant) instead of `signal.signal`.

**`ignored_files` statistic counter is never incremented:**
- Issue: `_stats["ignored_files"]` is initialized and reset but never incremented anywhere. `_should_ignore_file` returns True/False but its callers do not update this counter.
- Files: `aws_copier/core/file_listener.py` (lines 99, 238, 530)
- Impact: The stats dict always reports `ignored_files: 0`, making it useless for diagnostics.
- Fix approach: Increment `self._stats["ignored_files"]` in `_scan_current_files` when `_should_ignore_file` returns True.

**Duplicate ignore pattern sets across classes:**
- Issue: `FileListener` and `FileChangeHandler` each define their own `ignore_patterns` sets with slightly different contents. `FileChangeHandler` includes `$RECYCLE.BIN` and `System Volume Information` in its patterns set (not dirs set) while `FileListener` puts them in `ignore_dirs`. The two classes can diverge silently.
- Files: `aws_copier/core/file_listener.py` (lines 40-77), `aws_copier/core/folder_watcher.py` (lines 38-77)
- Impact: A file ignored by the batch scanner may be uploaded by the real-time watcher or vice versa.
- Fix approach: Extract ignore patterns into a shared module-level constant or a dedicated `IgnoreConfig` dataclass consumed by both classes.

**Glob patterns in `ignore_patterns` are never matched as globs:**
- Issue: `ignore_patterns` contains `"*.pyc"`, `"*.bak"`, `"*.backup"`, and `"*~"` with glob-style wildcards. The `_should_ignore_file` method only does exact set membership and `startswith` checks — it never applies glob matching. Files named `report.bak` or `script.pyc` will NOT be ignored.
- Files: `aws_copier/core/file_listener.py` (lines 61-74, 466-486)
- Impact: Files intended to be excluded from backup (compiled Python, backup copies) are silently uploaded to S3.
- Fix approach: Use `fnmatch.fnmatch(filename, pattern)` for patterns containing `*`.

## Known Bugs

**`_should_ignore_file` returns False for symlinks with dot-prefix logic:**
- Issue: The dot-prefix check in `_should_ignore_file` (line 483) only triggers for patterns that start with `.`, but hidden files like `.env` that don't match any pattern are not ignored. Meanwhile `_should_ignore_directory` skips all dot-prefix directories — the two methods have inconsistent hidden-item policies.
- Files: `aws_copier/core/file_listener.py` (lines 466-486, 488-515)
- Trigger: Any hidden file (e.g., `.env`, `.npmrc`) inside a watch folder will be uploaded to S3.
- Workaround: None.

**`call_soon_threadsafe(asyncio.create_task, coro)` is incorrect:**
- Issue: In `FileChangeHandler.on_any_event`, the call `self.event_loop.call_soon_threadsafe(asyncio.create_task, self._process_changed_file(...))` passes `asyncio.create_task` as the callback but `asyncio.create_task` requires a running event loop at the call site — it will use the current thread's loop, not `self.event_loop`. This is a known asyncio anti-pattern that causes `RuntimeError: no running event loop` or schedules the task on the wrong loop.
- Files: `aws_copier/core/folder_watcher.py` (lines 101-103)
- Trigger: Any file system change event when the watcher is running.
- Workaround: Replace with `self.event_loop.call_soon_threadsafe(self.event_loop.create_task, self._process_changed_file(...))` or use `asyncio.run_coroutine_threadsafe`.

**`asyncio.get_event_loop()` is deprecated in Python 3.10+:**
- Issue: `S3Manager._calculate_md5` uses `asyncio.get_event_loop()` (line 204) which is deprecated and emits a `DeprecationWarning` in Python 3.10+ when called from a coroutine without a running loop. The project supports Python >=3.9.
- Files: `aws_copier/core/s3_manager.py` (lines 204-205)
- Trigger: Any MD5 calculation via `S3Manager._calculate_md5`.
- Workaround: Replace with `asyncio.get_running_loop()`.

**`_load_backup_info` uses sync `open()` inside an async method:**
- Issue: `_load_backup_info` is declared `async` but uses synchronous `open()` (line 215) instead of `aiofiles`. This blocks the event loop during file reads. Separately, `_update_backup_info` also uses synchronous `open()` (line 425).
- Files: `aws_copier/core/file_listener.py` (lines 202-220, 415-429)
- Trigger: Every folder scan on slow or network-mounted disks.
- Workaround: Not critical for local SSDs, but breaks the async guarantee.

**Upload proceeds serially despite concurrent task creation:**
- Issue: In `_upload_files`, tasks are created for all files concurrently, but then awaited one-by-one in a `for` loop with individual `wait_for` calls (lines 365-380). This destroys the concurrency benefit — files upload serially, and the semaphore (50 slots) is never actually exercised concurrently.
- Files: `aws_copier/core/file_listener.py` (lines 354-388)
- Trigger: Every upload batch with more than one file.
- Fix approach: Replace the serial loop with `asyncio.gather(*tasks, return_exceptions=True)`.

## Security Considerations

**AWS credentials stored in plaintext YAML:**
- Risk: `config.yaml` stores `aws_access_key_id` and `aws_secret_access_key` in plaintext. `SimpleConfig.save_to_yaml` and `to_dict()` both serialize the secret key. Any process or user with read access to `config.yaml` obtains full AWS credentials.
- Files: `aws_copier/models/simple_config.py` (lines 64-65, 97-98), `config.yaml`
- Current mitigation: None — the example `config.yaml` is in the project root, not in a protected directory.
- Recommendations: Support reading credentials from environment variables or AWS credential chain (`~/.aws/credentials`) instead of storing in YAML. At minimum, document that `config.yaml` must have restricted file permissions (chmod 600).

**No credential validation before config is saved:**
- Risk: `SimpleConfig` defaults `aws_access_key_id` to the literal string `"YOUR_ACCESS_KEY_ID"`. `save_to_yaml` writes placeholder credentials to disk. A misconfigured instance will save dummy secrets that may confuse auditing.
- Files: `aws_copier/models/simple_config.py` (lines 15-16)
- Current mitigation: None.
- Recommendations: Add a `validate()` method that checks credentials look like real AWS keys before allowing them to be used or saved.

**MD5 used for integrity, not collision-resistant hashing:**
- Risk: The system uses MD5 (`hashlib.md5`) to verify file integrity for both local change detection and S3 upload verification. MD5 is cryptographically broken — a malicious actor with write access to watched folders could craft a file that passes MD5 checks while containing different content.
- Files: `aws_copier/core/file_listener.py` (line 453), `aws_copier/core/s3_manager.py` (line 195)
- Current mitigation: MD5 collision attacks require intentional crafting; risk is low for personal backup use.
- Recommendations: Switch to SHA-256 for local change detection. For S3 verification, use the native S3 checksum feature (CRC32/SHA-256 via `ChecksumAlgorithm`).

**Sensitive file paths silently uploaded:**
- Risk: Hidden files like `.env`, `.npmrc`, and SSH keys inside watched folders are not excluded by the ignore logic (see bug above). These can contain credentials that get backed up to S3.
- Files: `aws_copier/core/file_listener.py` (lines 466-486)
- Current mitigation: None.
- Recommendations: Add a default deny-list for known sensitive file patterns (`.env*`, `*.pem`, `id_rsa`, etc.) and make it configurable.

## Performance Bottlenecks

**Backup info file is read and rewritten on every folder scan pass:**
- Problem: For each folder, `_process_current_folder` reads the entire `.milo_backup.info` JSON, recomputes all MD5s, then rewrites the entire JSON. For folders with thousands of files, this is O(n) disk I/O per scan cycle.
- Files: `aws_copier/core/file_listener.py` (lines 154-200)
- Cause: No in-memory cache of backup state between scans; no incremental update of individual entries.
- Improvement path: Keep `existing_backup_info` in memory between scans; only rewrite the JSON file when entries actually change.

**MD5 computed twice for every file upload:**
- Problem: `_upload_single_file` calls `_calculate_md5` (line 315), then `s3_manager.upload_file` also calls `_calculate_md5` internally (line 93 in `s3_manager.py`). Every uploaded file has its MD5 computed twice.
- Files: `aws_copier/core/file_listener.py` (lines 296-340), `aws_copier/core/s3_manager.py` (lines 77-136)
- Cause: Duplicate MD5 logic in two layers with no cache sharing.
- Improvement path: Pass the already-computed MD5 hash to `upload_file` as a parameter instead of recomputing.

**No debouncing on real-time file change events:**
- Problem: `FileChangeHandler.on_any_event` triggers `_process_current_folder` immediately on every `created` or `modified` event. Applications that do atomic-save (write temp file + rename) generate 2-3 events per save. Rapidly-edited files trigger full folder scans repeatedly.
- Files: `aws_copier/core/folder_watcher.py` (lines 79-106)
- Cause: No event coalescing or debounce timer.
- Improvement path: Queue events with a short debounce window (e.g., 2 seconds) and process only the latest event per file.

## Fragile Areas

**Thread/loop coordination between GUI thread and background asyncio loop:**
- Files: `main_gui.py` (lines 78-106, 149-161), `aws_copier/ui/simple_gui.py` (lines 235-252)
- Why fragile: `AWSCopierGUIApp` stores `self.loop` from the background thread and calls `self.loop.call_soon_threadsafe` from the main (Tkinter) thread and from signal handlers. If the background loop is closed before the GUI finishes (race condition in cleanup), this raises `RuntimeError: Event loop is closed`.
- Safe modification: Always check `self.loop and not self.loop.is_closed()` before calling `call_soon_threadsafe`. The shutdown path in `_gui_shutdown_callback` already does this check but `_setup_signal_handlers` fallback path (line 160) does not.
- Test coverage: No integration tests cover the GUI + background loop interaction.

**`asyncio.BaseEventLoop` type annotation is deprecated:**
- Files: `aws_copier/core/folder_watcher.py` (line 22)
- Why fragile: `asyncio.BaseEventLoop` was deprecated as a public type in Python 3.10. Mypy and type checkers may flag this. Should be `asyncio.AbstractEventLoop`.

**Multipart upload has no progress tracking or resume capability:**
- Files: `aws_copier/core/s3_manager.py` (lines 277-362)
- Why fragile: If a multipart upload is interrupted mid-way (timeout, network drop), the `abort_multipart_upload` cleanup runs, but incomplete multipart uploads incur S3 storage costs until AWS lifecycle rules clean them up. No resume logic exists.
- Safe modification: Add an S3 lifecycle rule to abort incomplete multipart uploads after 1 day. Log the upload ID so interrupted uploads can be identified.

**`_process_folder_recursively` uses Python recursion for deep trees:**
- Files: `aws_copier/core/file_listener.py` (lines 123-152)
- Why fragile: Deep folder hierarchies (>500 levels) will hit Python's default recursion limit (1000). This is unlikely in practice but is a structural concern.
- Safe modification: Convert to an iterative approach using a queue/stack (`collections.deque`).

## Scaling Limits

**S3 connection pool and concurrency ceiling:**
- Current capacity: `AioConfig(max_pool_connections=100)` in `S3Manager`. `FileListener` semaphores are hardcoded to 50.
- Limit: With the serial `await` loop bug in `_upload_files`, effective concurrency is 1 regardless of pool size.
- Scaling path: Fix the serial loop bug; wire `max_concurrent_uploads` from config to the semaphore.

**No rate limiting for S3 API calls:**
- Current capacity: Unbounded — `check_exists` issues one `HeadObject` call per file before each upload.
- Limit: AWS S3 has a default limit of 3500 PUT/COPY/POST/DELETE and 5500 GET/HEAD requests per second per prefix. Large initial scans on flat bucket structures could hit throttling.
- Scaling path: Use the S3 server-side checksum feature to avoid `HeadObject` pre-checks, or implement exponential backoff on throttle errors.

## Dependencies at Risk

**`ruff` listed as a runtime dependency:**
- Risk: `pyproject.toml` (line 13) lists `ruff>=0.12.11` under `[project.dependencies]` (runtime), not under `[project.optional-dependencies].dev`. This adds a linting tool as a mandatory runtime dependency, increasing install size unnecessarily.
- Impact: All users must install ruff even if they only run the application.
- Migration plan: Move `ruff` to `[dependency-groups].dev` or `[project.optional-dependencies].dev`.

**`python-dotenv` is a runtime dependency with no use in production code:**
- Risk: `python-dotenv` is in runtime dependencies but is only used in `s3_manager.py`'s `if __name__ == "__main__"` script block (the dev test harness at the bottom of the file).
- Impact: Unnecessary runtime dependency; also, the test harness in production code is an anti-pattern.
- Migration plan: Move `python-dotenv` to dev dependencies; extract the `if __name__ == "__main__"` block out of `s3_manager.py` into a separate dev script.

## Test Coverage Gaps

**No integration tests with real (or mocked via moto) S3:**
- What's not tested: End-to-end upload flow, credential handling, bucket access errors, multipart upload, and S3 existence check logic.
- Files: `tests/unit/test_s3_manager.py` — most S3 tests mock the aiobotocore client directly; `moto[s3]` is listed as a dev dependency but never used.
- Risk: S3 behavior changes or aiobotocore API differences go undetected.
- Priority: High

**No tests for `FolderWatcher` or `FileChangeHandler`:**
- What's not tested: Real-time event processing, the thread-to-async handoff via `call_soon_threadsafe`, debounce behavior (absent), and watcher lifecycle (start/stop).
- Files: `aws_copier/core/folder_watcher.py` — no corresponding test file exists.
- Risk: The `asyncio.create_task` threading bug (documented above) cannot be caught by tests.
- Priority: High

**No tests for GUI layer:**
- What's not tested: Shutdown callback propagation, log display, signal handling, and thread coordination in `AWSCopierGUIApp`.
- Files: `main_gui.py`, `aws_copier/ui/simple_gui.py` — `test_gui.py` exists at the root but is not under `tests/` and is not run by pytest.
- Risk: GUI shutdown race conditions go undetected.
- Priority: Medium

**Coverage threshold is set to 30%:**
- What's not tested: The low threshold (`--cov-fail-under=30` in `pyproject.toml` line 82) means CI passes with most code untested.
- Files: `pyproject.toml` (line 82)
- Risk: Regressions in core backup logic can ship undetected.
- Priority: Medium

---

*Concerns audit: 2026-04-24*
