# Requirements

**Project:** AWS Copier
**Generated:** 2026-04-24
**Status:** Active

---

## v1 Requirements — Core Reliability Milestone

### Async Correctness

- [ ] **ASYNC-01**: Daemon correctly bridges watchdog file events to the asyncio event loop using `asyncio.run_coroutine_threadsafe`, replacing the `call_soon_threadsafe(asyncio.create_task, coro)` anti-pattern that drops all real-time events on Python 3.10+
- [ ] **ASYNC-02**: File uploads run concurrently up to the configured limit using `asyncio.gather` with per-task timeouts and `return_exceptions=True`, replacing the serial `for` loop that gives effective concurrency of 1
- [ ] **ASYNC-03**: Backup state file reads and writes (`_load_backup_info` / `_update_backup_info`) use `aiofiles` instead of blocking `open()`, with a per-folder `asyncio.Lock` to prevent read-modify-write races
- [ ] **ASYNC-04**: `asyncio.get_event_loop()` calls replaced with `asyncio.get_running_loop()` or `asyncio.to_thread()` to eliminate `DeprecationWarning` on Python 3.10+
- [ ] **ASYNC-05**: `asyncio.BaseEventLoop` type annotation replaced with `asyncio.AbstractEventLoop` throughout
- [ ] **ASYNC-06**: Signal handling re-enabled in headless mode using `loop.add_signal_handler` (Unix) with a `sys.platform == "win32"` fallback via `signal.signal` + `loop.call_soon_threadsafe`, so SIGTERM/SIGINT triggers a clean shutdown instead of killing mid-upload

### Ignore Rules

- [ ] **IGNORE-01**: Glob patterns in the ignore list (e.g. `*.pyc`, `*.bak`, `*~`) are evaluated using `fnmatch.fnmatch`, not exact set membership, so files like `report.bak` and `script.pyc` are correctly excluded
- [ ] **IGNORE-02**: Hidden dot-prefix files (`.env`, `.npmrc`, SSH keys, `*.pem`, `id_rsa`, `*.key`) are blocked from upload by a hardcoded deny-list in `should_ignore_file`, addressing the asymmetry with `should_ignore_directory`
- [ ] **IGNORE-03**: Ignore rules are defined once in a shared `aws_copier/core/ignore_rules.py` frozen dataclass (`IgnoreRules`) and consumed by both `FileListener` and `FileChangeHandler`, eliminating the diverging duplicate sets
- [ ] **IGNORE-04**: `_stats["ignored_files"]` counter is correctly incremented when `should_ignore_file` returns `True`, so stats reports show actual ignored file counts

### Configuration & Cleanup

- [ ] **CONFIG-01**: `max_concurrent_uploads` from `config.yaml` is wired to the `upload_semaphore` in `FileListener.__init__`, so users can actually control concurrency
- [ ] **CONFIG-02**: The `aws-copier` CLI entrypoint in `pyproject.toml` points to a real module (`main:sync_main` or `main_gui:main`), so `uv run aws-copier` works
- [ ] **CONFIG-03**: `discovered_files_folder` dead config field and its `create_directories()` call are removed from `SimpleConfig`
- [ ] **CONFIG-04**: `ruff` and `python-dotenv` moved from runtime dependencies to dev dependencies

---

## v2 Requirements — Performance & Polish Milestone

### Performance

- [ ] **PERF-01**: Before computing MD5, the scanner checks whether a file's modification timestamp (`st_mtime`) has changed since the last backup; files with unchanged mtime are skipped without MD5 computation, reducing CPU and I/O for unchanged files. The `.milo_backup.info` format is extended to store `{filename: {md5: "...", mtime: ...}}` with backward-compatible migration for existing entries.
- [ ] **PERF-02**: Backup info state (`existing_backup_info`) is cached in memory between scan cycles; the on-disk `.milo_backup.info` is only re-read when the file's `st_mtime` changes, avoiding O(n) disk reads per 5-minute cycle for unchanged folders
- [ ] **PERF-03**: MD5 computed once per file upload; the pre-computed hash is passed to `S3Manager.upload_file` instead of recomputed inside it, eliminating the duplicate MD5 call
- [ ] **PERF-04**: Real-time file change events are debounced with a 2-second per-path timer; rapid successive events for the same file (atomic-save patterns) trigger only one `_process_current_folder` call

### Configuration

- [ ] **CONFIG-05**: AWS credentials can be loaded from the standard provider chain (`~/.aws/credentials`, `AWS_*` env vars) when not present in `config.yaml`, so plaintext credentials in config are optional rather than required
- [ ] **CONFIG-06**: Per-directory `.backupignore` files (gitignore-style, via `pathspec` library) allow per-folder custom ignore patterns, as a complement to the global `IgnoreRules`
- [ ] **CONFIG-07**: At daemon startup, an S3 lifecycle rule is checked (or set) on the configured bucket to abort incomplete multipart uploads after 1 day, protecting against indefinite cost accumulation from interrupted uploads

---

## Out of Scope

- **S3 versioning / restore / download** — handled at the S3 bucket level; no application code needed
- **Client-side encryption** — use S3 SSE; application-level encryption adds key management complexity for no practical gain on a personal tool
- **Web UI or remote monitoring** — local daemon with optional tkinter GUI is the right model for a personal tool
- **Multi-user or team features** — single user, single machine
- **Multi-cloud support (GCS, Azure Blob)** — single-provider by design
- **Scheduling / cron** — always-on daemon is the intended model; use system cron or launchd externally if needed
- **SHA-256 migration for MD5** — MD5 collision attacks require intentional crafting; risk is negligible for personal backup use; not worth breaking the `.milo_backup.info` format
- **Upload resume with checkpoints** — S3 multipart abort + lifecycle rule is sufficient protection; full resume logic is complex and unnecessary for a personal tool

---

## Traceability

| REQ-ID | Phase | Status |
|--------|-------|--------|
| ASYNC-01 | Phase 1 | Pending |
| ASYNC-02 | Phase 1 | Pending |
| ASYNC-03 | Phase 1 | Pending |
| ASYNC-04 | Phase 1 | Pending |
| ASYNC-05 | Phase 1 | Pending |
| ASYNC-06 | Phase 1 | Pending |
| IGNORE-01 | Phase 1 | Pending |
| IGNORE-02 | Phase 1 | Pending |
| IGNORE-03 | Phase 1 | Pending |
| IGNORE-04 | Phase 1 | Pending |
| CONFIG-01 | Phase 1 | Pending |
| CONFIG-02 | Phase 1 | Pending |
| CONFIG-03 | Phase 1 | Pending |
| CONFIG-04 | Phase 1 | Pending |
| PERF-01 | Phase 2 | Pending |
| PERF-02 | Phase 2 | Pending |
| PERF-03 | Phase 2 | Pending |
| PERF-04 | Phase 2 | Pending |
| CONFIG-05 | Phase 2 | Pending |
| CONFIG-06 | Phase 2 | Pending |
| CONFIG-07 | Phase 2 | Pending |

---

## v2 Requirements — Audit Hardening Milestone (Phase 3, from the 2026-11-23 ponytail audit)

- [ ] **TEST-01**: Lifecycle tests never bind the real dashboard port; suite is green while port 8765 is busy
- [ ] **SAFE-01**: A tracked file is deleted from S3 only when it no longer exists locally; hash failures and ignore-rule matches keep the entry (audit #3)
- [ ] **SAFE-02**: Soft-deleting a key whose S3 object is already gone succeeds, so the entry leaves `.milo_backup.info` (audit #5)
- [ ] **SAFE-03**: A failed move whose S3 source is confirmed absent falls back to a normal upload in the same scan (audit #2)
- [ ] **SAFE-04**: A move never deletes the source key while the old local file still exists (copy-only) (audit #1)
- [ ] **SCAN-01**: The recorded mtime is the one read before hashing, so a mid-hash edit is re-detected (audit #4)
- [ ] **SCAN-02**: The scan's MD5 is reused for upload; a changed file is hashed once (audit #9)
- [ ] **S3-01**: Upload verification compares real data (ETag for single-part) instead of self-written metadata (audit #12)
- [ ] **DUR-01**: `.milo_backup.info` is written atomically (tmp + replace) and proven to work on the NAS mount (audit #6)
- [ ] **HYG-01**: Dashboard is not exposed beyond what the user chooses (audit #10)
- [ ] **HYG-02**: Dead `s3_manager.main()` dev harness that prints secrets is removed (audit #11)
- [ ] **HYG-03**: Stale docstring in `delete_scattered_duplicates.py` corrected (audit #16)
- [ ] **PERF-09**: Per-folder rebuild uses sets, not list membership (audit #13)
- [ ] **PERF-10**: Directory listing and stat run off the event loop (audit #14)
- [ ] **DEP-01**: Pillow becomes an optional extra; aiofiles replaced by `asyncio.to_thread` (audit #15)

| REQ-ID | Phase | Status |
|--------|-------|--------|
| TEST-01, SAFE-01..04, SCAN-01..02, S3-01, DUR-01, HYG-01..03, PERF-09..10, DEP-01 | Phase 3 | Pending |
