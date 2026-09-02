---
name: aws-copier-architecture
description: This skill should be used when working on the aws-copier codebase itself — modifying FileListener, S3Manager, SimpleConfig, IgnoreRules, or the web dashboard, debugging sync/upload issues, or answering "how does aws-copier work" / "where is X handled" questions. Provides an up-to-date module map and design-decision reference that supplements (and in places corrects) CLAUDE.md.
version: 1.0.0
---

# AWS Copier Architecture Reference

Personal async Python daemon that periodically backs up local folders to S3 incrementally, using per-directory MD5 tracking to skip already-synced files. Two run modes: headless CLI (`main.py`) or tkinter GUI (`main_gui.py`).

**No real-time file watcher.** An earlier version used a `watchdog`-based real-time watcher
(`FolderWatcher`/`FileChangeHandler` in `aws_copier/core/folder_watcher.py`) alongside
periodic scans; it was removed entirely. Both real production bugs found in this codebase
were in that subsystem specifically — debounce keyed per-file instead of per-folder (a
multi-file copy into one folder fired one redundant, overlapping `_process_current_folder`
call per file instead of one, causing genuinely duplicated concurrent uploads), and files
inside ignored directories (e.g. `.sync/`) slipping through because the watcher only checked
a changed file's own name, never its ancestor directories. On top of those two bugs, a
NAS-bundled cloud-sync tool continuously touching the whole watched tree made `inotify`
event-queue overflow (and therefore silently missed files) a standing risk. For a personal
backup tool, bounded periodic-scan latency was judged the better tradeoff over patching
around an entire watcher subsystem's worth of edge cases one at a time. If you see any
reference to `FolderWatcher`, `FileChangeHandler`, `watchdog`, or `should_ignore_path` in
docs, comments, or old context, it's stale — that code no longer exists.

## Module map

| Module | Role |
|---|---|
| `aws_copier/models/simple_config.py` — `SimpleConfig`, `load_config()` | YAML-backed settings. Supports legacy `watch_folders` list or new dict (`{folder: s3_name}`). Auto-detects credential-chain fallback (`use_credential_chain`) when explicit key/secret are absent. Also holds `web_port`/`web_enabled` (dashboard) — **not documented in CLAUDE.md**. |
| `aws_copier/core/file_listener.py` — `FileListener` | Central orchestrator. Recursively scans watch folders in **randomized subdir order** (prevents starving deep trees across runs) and processes sibling folders **concurrently** (`folder_semaphore`, default 20 — CONC-02), not one at a time. Tracks per-folder `.milo_backup.info` JSON, `{md5, mtime, s3_key}` dicts (migrated on read from legacy formats via `_migrate_entry`). Uses `st_mtime` to skip re-hashing unchanged files (PERF-01). Cascades `.backupignore` (gitignore-style via `pathspec`) from watch-root down to each subdirectory — **not documented in CLAUDE.md at all**. Captures `st_mtime` *immediately before* the upload call, not at scan time, so a file modified mid-upload is caught on the next cycle. Detects a folder rename/move (MOVE-01: stale recorded `s3_key` vs freshly computed one) and reconciles via `S3Manager.move_object()` (server-side copy) instead of re-uploading. Semaphores: `upload_semaphore` (config-driven, default 10), `md5_semaphore` (fixed 10), `folder_semaphore` (fixed 20). Per-folder `asyncio.Lock` registry (`_folder_locks`) guards only the individual `_load_backup_info`/`_update_backup_info` I/O steps — **not** the whole scan+upload pipeline, so two calls for the same folder can still interleave if triggered independently (this was the root cause of the debounce bug above). `_active_upload_tasks` set lets shutdown drain in-flight uploads. `clear_caches()` drops the `_backup_info_cache`/`_backup_info_mtime` dicts (MEM-01) — the main unbounded-memory-growth point for a large library — called every 5 minutes from `main.py`'s status loop. |
| `aws_copier/core/s3_manager.py` — `S3Manager` | Async S3 wrapper via `aiobotocore`. Files ≤100MB use `put_object`; >100MB use multipart upload — parts are `_MULTIPART_CHUNK_SIZE` (16MB, not S3's 5MB minimum) uploaded **concurrently** up to `_MULTIPART_CONCURRENCY` (8) at once via a sliding window (PERF-06/07; the old code uploaded parts strictly serially, capping per-file throughput at roughly one chunk per network round-trip). `abort_multipart_upload` on any part failure, with in-flight parts explicitly cancelled first. MD5 stored as S3 object `Metadata["md5-checksum"]` and verified both after upload and via `check_exists`. `move_object()` does a server-side `CopyObject` + delete-old-key for folder-rename reconciliation (MOVE-01). `estimate_upload_timeout(file_size)` scales the per-file timeout with size (1MB/s assumed floor + 60s buffer, 300s minimum) instead of a fixed 300s. `ensure_lifecycle_rule()` sets an `AbortIncompleteMultipartUpload` (1 day) lifecycle rule on startup — but never overwrites a bucket's pre-existing lifecycle config; only adds one if none exists. |
| `aws_copier/core/ignore_rules.py` — `IGNORE_RULES` singleton | Centralized ignore policy used by `FileListener`. `should_ignore_file`: all dotfiles ignored, plus a security-conscious deny list (`.env*`, `*.pem`, `*.key`, SSH private keys, `.npmrc`, `.pypirc`, `.netrc`, `*.secret`) and generic junk (`.DS_Store`, `*.tmp`, `*.pyc`, ...). `should_ignore_dir`: skips VCS/build/cache dirs and symlinked dirs entirely — checked before recursing into a directory during the batch scan, so ignored directories are never even listed. |
| `aws_copier/web/dashboard.py` — `WebDashboard` | `aiohttp`-based live log/status dashboard. Started early in `AWSCopierApp.start()` (so log capture begins immediately) and stopped last during shutdown (so final log lines remain visible). **Entirely absent from CLAUDE.md's layer list.** |
| `aws_copier/ui/simple_gui.py` — `AWSCopierGUI`, `LogHandler` | tkinter GUI: log display + shutdown control, driven by `main_gui.py`'s background asyncio thread. |
| `main.py` — `AWSCopierApp` | Headless entry point. Startup order: signal handlers → web dashboard → `S3Manager.initialize()` (30s timeout) → `ensure_lifecycle_rule()` → full `scan_all_folders()` → 5-minute status loop (stats log → `clear_caches()`/`gc.collect()`/`_trim_memory()` (MEM-01) → `_maybe_run_periodic_rescan()` (RELIAB-01: re-runs `scan_all_folders()` once `FULL_RESCAN_INTERVAL_SECONDS`, default 6h, has elapsed — the sole sync mechanism, see module docstring) → wait on shutdown event, 300s timeout). Shutdown order: drain in-flight uploads (60s max, abandons + logs the rest) → close S3 client → stop dashboard. Windows: disables console QuickEdit mode so the process never freezes on an accidental click; signal handling falls back to `signal.signal` + `call_soon_threadsafe` since `loop.add_signal_handler` isn't supported on `ProactorEventLoop`. |
| `upload_large.py` | Standalone one-shot script for bulk-uploading a large file/directory tree to S3 outside the daemon (recurses subdirectories, preserves relative path in the S3 key; has its own independent concurrent-multipart implementation, sliding-window style, predating and matching `S3Manager`'s later PERF-06 fix). |

## Key design decisions worth knowing before changing anything

- **MD5 is the source of truth for "changed"**; `mtime` is only a fast-skip cache to avoid re-hashing. If `mtime` matches the stored value, MD5 is *not* recomputed — the stored MD5 is carried forward as-is. Bypassing or invalidating the `.milo_backup.info` mtime cache is the way to force a re-hash.
- **`st_mtime` is captured twice with different purposes**: once during scan (for the skip-cache) and again immediately before the actual `upload_file` call (recorded as the authoritative mtime in `.milo_backup.info`), so a file edited during a slow upload is correctly re-detected next cycle rather than falsely marked in-sync.
- **`.backupignore` cascades additively** down the tree — a child directory's `.backupignore` adds to, not replaces, its ancestors' rules (root → parent → child order). The match input must be the path relative to the watch root with forward slashes; passing an absolute path silently fails to match.
- **Randomized subdirectory traversal order** (`random.shuffle(subdirs)`) is deliberate — it's a cheap mitigation against one deep/slow subtree always going last (or first) across repeated runs.
- **Per-folder `asyncio.Lock`**, not a single global lock, guards `.milo_backup.info` I/O — but only the individual load/write steps, not the whole scan+upload pipeline. Two independently-triggered calls for the *same* folder (e.g. a stray reference to something scheduling a second `_process_current_folder` for a folder already being processed) can still interleave and duplicate work; this is exactly what the old per-file debounce bug did.
- **Shutdown never raises**: signal handling, upload draining, and cleanup are wrapped so a slow/failed drain logs a warning (naming the abandoned file) rather than blocking exit indefinitely.

## Deployment: Docker (primary target, e.g. UGreen NAS)

The daemon now ships with `Dockerfile`, `docker-compose.yml`, and `docker/entrypoint.sh` for headless deployment on a NAS. Key points:

- The image only installs dependencies (`uv sync --frozen --no-install-project --no-dev`) and runs `python main.py` directly — it does **not** install the `aws-copier` package itself, since that requires `README.md` (excluded via `.dockerignore`) to satisfy hatchling's readme-field validation. If a future change needs the console-script entry point inside the container, either stop excluding `README.md` or drop `--no-install-project`.
- `docker/entrypoint.sh` creates a user matching the `PUID`/`PGID` env vars at container start and `exec gosu`s into it, so files the daemon writes on NAS bind mounts (`.milo_backup.info`) aren't owned by root.
- `config.yaml`'s `watch_folders` keys must be **container** paths matching `docker-compose.yml`'s mount targets, not host/NAS paths — the host-side NAS paths only ever appear on the left of the `:` in `docker-compose.yml`'s `volumes:` list.
- `aws_copier/ui/` (tkinter GUI) is excluded from the build context; `main_gui.py`/`test_gui.py`/`setup_windows.py` stay host-only, dev-mode artifacts.

**Fixed bug found while wiring this up**: `main.py`'s `main()` used to gate on a local `config.yaml` existing, but `AWSCopierApp.__init__` called `load_config()` with no argument — which defaults to `~/aws-copier-config.yaml`, silently ignoring the local file it had just checked for. Fixed by threading `config_path` through `AWSCopierApp.__init__` into `load_config()`. Worth remembering if `AWSCopierApp` construction changes again — the local `config.yaml` path and the actual config load must stay in sync.

## Known documentation gaps

CLAUDE.md's "Technology Stack" / "Architecture" sections predate several features and are missing: the `pathspec`, `aiohttp`, and `tqdm` dependencies; the `.backupignore` feature and `ignore_rules.py` module; and the entire `aws_copier/web/dashboard.py` web dashboard layer. Treat CLAUDE.md's architecture description as a starting point, not ground truth — cross-check against this skill or the source when in doubt.
