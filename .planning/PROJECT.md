# AWS Copier

## What This Is

A personal Python daemon that watches local folders and incrementally backs them up to AWS S3. It runs in two modes — headless CLI or optional tkinter GUI — and uses per-directory MD5 tracking to skip already-synced files, keeping S3 in sync with minimal redundant uploads.

## Core Value

Files in watched folders are reliably, completely synced to S3 — nothing silently missed, nothing corrupted mid-upload.

## Requirements

### Validated

- ✓ Incremental backup scan using per-directory `.milo_backup.info` MD5 tracking — existing
- ✓ Real-time folder watching via watchdog, routing events into asyncio event loop — existing
- ✓ Async S3 uploads with multipart support for files >100MB — existing
- ✓ Concurrent MD5 computation and uploads via asyncio semaphores — existing
- ✓ Headless CLI mode (`main.py`) with 5-minute status loop — existing
- ✓ Optional tkinter GUI mode (`main_gui.py`) with log display and shutdown control — existing
- ✓ YAML config for watch folders, AWS credentials, and concurrency settings — existing
- ✓ S3 existence check before upload to skip already-synced files — existing
- ✓ Cross-platform support: macOS, Linux, Windows — existing

### Active

- [ ] Fix serial upload bug — tasks created concurrently but awaited one-by-one; replace with `asyncio.gather`
- [ ] Fix real-time watcher thread bridge — `call_soon_threadsafe(asyncio.create_task, coro)` anti-pattern causes wrong-loop errors
- [ ] Fix glob patterns in ignore list — `*.pyc`, `*.bak` etc. never matched as globs; use `fnmatch`
- [ ] Fix hidden file leakage — `.env`, `.npmrc` and other dot-files in watch folders are uploaded to S3
- [ ] Wire `max_concurrent_uploads` config to semaphore — currently hardcoded to 50, config field has no effect
- [ ] Fix broken `aws-copier` CLI entrypoint in `pyproject.toml` — points to non-existent `simple_main:main`
- [ ] Fix `ignored_files` stat counter — never incremented, always reports 0
- [ ] Deduplicate ignore patterns — `FileListener` and `FileChangeHandler` maintain separate diverging sets
- [ ] Fix sync file I/O in async methods — `_load_backup_info` and `_update_backup_info` use blocking `open()`
- [ ] Re-enable signal handling in headless mode — `_setup_signal_handlers()` call is commented out, preventing graceful shutdown

### Out of Scope

- Multi-user or team features — personal tool only
- Cloud providers other than AWS S3 — single-provider by design
- Web UI or remote monitoring — local daemon with optional tkinter GUI is sufficient
- Scheduling/cron orchestration — always-on daemon is the intended model

## Context

The codebase is complete and functional. The main areas to improve are correctness bugs (silent upload failures, wrong-loop errors), performance (serial uploads defeating the semaphore), and reliability (graceful shutdown, sync I/O blocking the event loop). The tech debt items in `.planning/codebase/CONCERNS.md` provide detailed analysis of each issue.

**Stack:** Python 3.11 + asyncio, aiobotocore, watchdog, aiofiles, pyyaml, tkinter (stdlib)
**Tooling:** uv, ruff, pytest + pytest-asyncio, moto[s3] for S3 mocking

## Constraints

- **Tech stack**: Python 3.11 + asyncio — all improvements must preserve async-first design
- **Compatibility**: Must run on macOS, Linux, Windows — no platform-specific syscalls without fallback
- **Packaging**: Single `uv`-managed project; no containerization

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Per-directory `.milo_backup.info` for state | Avoids S3 list calls on every scan; local state is fast | ✓ Good |
| aiobotocore over boto3 | True async S3 calls; boto3 would block the event loop | ✓ Good |
| Plaintext AWS credentials in config.yaml | Simple for personal use; IAM roles add complexity for one user | — Pending |
| Separate asyncio loops for GUI and background I/O | tkinter requires main thread; asyncio can't share a loop across threads | ✓ Good |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-24 after initialization*
