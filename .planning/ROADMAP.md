# Roadmap: AWS Copier

## Overview

Two phases deliver the full improvement set. Phase 1 closes every correctness gap — the daemon currently silently drops real-time events, uploads files serially, leaks dot-files to S3, and cannot shut down cleanly. Phase 2 builds on that reliable foundation to add performance optimisations, credential hygiene, and per-directory ignore customisation.

## Phases

**Phase Numbering:**
- Integer phases (1, 2): Planned milestone work
- Decimal phases (1.1, 1.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Core Reliability** - Fix all correctness bugs so the daemon reliably syncs files in real time without leaking secrets or crashing on shutdown
- [ ] **Phase 2: Performance & Polish** - Add scan-time optimisations, credential chain support, per-directory ignore files, and multipart lifecycle protection

## Phase Details

### Phase 1: Core Reliability
**Goal**: The daemon correctly syncs files in real time — real-time events land in the event loop, uploads run concurrently, ignore patterns work as globs, dot-files never reach S3, and SIGTERM triggers a clean drain instead of a mid-upload kill
**Depends on**: Nothing (first phase)
**Requirements**: ASYNC-01, ASYNC-02, ASYNC-03, ASYNC-04, ASYNC-05, ASYNC-06, IGNORE-01, IGNORE-02, IGNORE-03, IGNORE-04, CONFIG-01, CONFIG-02, CONFIG-03, CONFIG-04
**Success Criteria** (what must be TRUE):
  1. A file saved in a watched folder while the daemon is running triggers an S3 upload without requiring the next 5-minute scan cycle
  2. When 10 files change simultaneously, all 10 upload concurrently up to the configured semaphore limit, not one-at-a-time
  3. A file named `report.bak` or `script.pyc` in a watched folder is never uploaded to S3
  4. A `.env` file or SSH key (`id_rsa`, `*.pem`) in a watched folder is never uploaded to S3
  5. Sending SIGTERM to the headless daemon waits for in-flight uploads to finish before exiting, and `uv run aws-copier` launches the daemon without error
**Plans**: 5 plans

Plans:
- [x] 01-01-PLAN.md — Create shared `aws_copier/core/ignore_rules.py` (IgnoreRules frozen dataclass + IGNORE_RULES singleton + tests) [IGNORE-01, IGNORE-02, IGNORE-03]
- [x] 01-02-PLAN.md — Config cleanup batch: fix pyproject.toml entrypoint, remove discovered_files_folder, move ruff/python-dotenv to dev deps [CONFIG-02, CONFIG-03, CONFIG-04]
- [x] 01-03-PLAN.md — folder_watcher.py refactor: run_coroutine_threadsafe bridge, AbstractEventLoop annotation, IGNORE_RULES consumption [ASYNC-01, ASYNC-05, IGNORE-03]
- [x] 01-04-PLAN.md — file_listener.py refactor: gather-based concurrent uploads, aiofiles+lock state I/O, config-wired semaphore, IGNORE_RULES consumption, ignored_files stat, active-upload-task set [ASYNC-02, ASYNC-03, ASYNC-04, IGNORE-03, IGNORE-04, CONFIG-01]
- [x] 01-05-PLAN.md — main.py signal handling: re-enable _setup_signal_handlers, platform-aware registration, 60-second graceful drain on SIGTERM [ASYNC-06]

### Phase 2: Performance & Polish
**Goal**: Scans run faster by skipping unchanged files, credentials can come from the standard AWS provider chain, per-directory `.backupignore` files control custom exclusions, and an S3 lifecycle rule prevents orphaned multipart parts from accumulating cost
**Depends on**: Phase 1
**Requirements**: PERF-01, PERF-02, PERF-03, PERF-04, CONFIG-05, CONFIG-06, CONFIG-07
**Success Criteria** (what must be TRUE):
  1. A folder with 1 000 unchanged files completes its 5-minute scan without recomputing MD5 for any file whose mtime has not changed since the last backup
  2. AWS credentials work when `config.yaml` has no `aws_access_key_id` field, falling back to `~/.aws/credentials` or `AWS_*` environment variables
  3. A `.backupignore` file placed in a watched subdirectory causes files matching its patterns to be excluded from uploads for that directory
  4. At daemon startup, the configured S3 bucket has an `AbortIncompleteMultipartUpload` lifecycle rule set (or confirmed present), visible in the startup log
**Plans**: 5 plans

Plans:
- [x] 02-01-PLAN.md — Add pathspec dep + SimpleConfig credential chain detection [CONFIG-05]
- [x] 02-02-PLAN.md — FileListener backup info format migration + mtime skip + in-memory cache [PERF-01, PERF-02]
- [x] 02-03-PLAN.md — S3Manager.upload_file precomputed_md5 + ensure_lifecycle_rule + chain-aware client kwargs [PERF-03, CONFIG-07]
- [x] 02-04-PLAN.md — FolderWatcher 2-second per-path event debounce [PERF-04]
- [ ] 02-05-PLAN.md — .backupignore cascade in FileListener + main.py lifecycle/credential-source wiring [CONFIG-06]

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Core Reliability | 0/5 | Not started | - |
| 2. Performance & Polish | 0/5 | Not started | - |
