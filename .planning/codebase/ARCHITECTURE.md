# Architecture

**Analysis Date:** 2026-04-24

## Pattern Overview

**Overall:** Event-driven pipeline with async I/O throughout

**Key Characteristics:**
- Full asyncio-based processing for all I/O (file reads, S3 operations)
- Two operating modes: headless CLI (`main.py`) and GUI with background thread (`main_gui.py`)
- Incremental backup using per-directory `.milo_backup.info` JSON files to track MD5 hashes
- Concurrency controlled via `asyncio.Semaphore` (upload: 50, MD5: 50 concurrent)
- Watchdog library bridges synchronous OS file events into the asyncio event loop via `call_soon_threadsafe`

## Layers

**Configuration Layer:**
- Purpose: Load, validate, and provide application settings
- Location: `aws_copier/models/simple_config.py`
- Contains: `SimpleConfig` class, `load_config()` factory
- Depends on: `pyyaml`
- Used by: All other layers (injected via constructor)

**Core Layer — File Listener:**
- Purpose: Incremental backup scanner; determines which files changed and orchestrates uploads
- Location: `aws_copier/core/file_listener.py`
- Contains: `FileListener` class
- Depends on: `S3Manager`, `SimpleConfig`, `aiofiles`
- Used by: Application entry points, `FolderWatcher` (calls `_process_current_folder` directly)

**Core Layer — Folder Watcher:**
- Purpose: Real-time OS filesystem event monitoring; routes events to `FileListener`
- Location: `aws_copier/core/folder_watcher.py`
- Contains: `FolderWatcher`, `FileChangeHandler` (watchdog handler)
- Depends on: `FileListener`, `SimpleConfig`, `watchdog`
- Used by: Application entry points

**Core Layer — S3 Manager:**
- Purpose: Async S3 client wrapper; handles upload, existence checks, multipart uploads
- Location: `aws_copier/core/s3_manager.py`
- Contains: `S3Manager` class
- Depends on: `aiobotocore`, `SimpleConfig`
- Used by: `FileListener` only

**UI Layer:**
- Purpose: Optional tkinter GUI providing log display and shutdown control
- Location: `aws_copier/ui/simple_gui.py`
- Contains: `AWSCopierGUI`, `LogHandler`, `create_gui()`
- Depends on: Python stdlib (`tkinter`, `logging`, `queue`, `threading`)
- Used by: `main_gui.py` only

**Entry Points:**
- `main.py` — headless asyncio runner (`AWSCopierApp`)
- `main_gui.py` — GUI runner with background thread (`AWSCopierGUIApp`)

## Data Flow

**Initial Backup Scan (both modes):**

1. `AWSCopierApp.start()` / `AWSCopierGUIApp._run_background_loop()` calls `S3Manager.initialize()` (validates S3 bucket connectivity)
2. `FileListener.scan_all_folders()` iterates `config.watch_folders`
3. For each folder, `_process_folder_recursively()` walks the directory tree
4. `_process_current_folder()` runs per directory:
   a. Load `.milo_backup.info` (JSON with `{filename: md5}` map)
   b. Scan current files, compute MD5 hashes in parallel (up to 50 concurrent via `md5_semaphore`)
   c. Compare MD5s — files with changed or missing hashes are queued for upload
   d. Upload changed files in parallel (up to 50 concurrent via `upload_semaphore`)
   e. Before upload, check S3 existence via `S3Manager.check_exists(key, md5)` to skip already-synced files
   f. After successful uploads, write updated `.milo_backup.info`

**Real-Time Monitoring:**

1. `FolderWatcher.start()` registers `FileChangeHandler` with watchdog `Observer` for each watch folder
2. Watchdog emits `on_any_event()` on the OS monitor thread for `created` / `modified` events
3. `FileChangeHandler.on_any_event()` calls `event_loop.call_soon_threadsafe(asyncio.create_task, ...)` to bridge into the async loop
4. `_process_changed_file()` calls `FileListener._process_current_folder(file.parent)` — reuses the same incremental logic

**GUI Mode — Threading:**

1. `asyncio.run(app.run())` starts the event loop on the main thread for initialization
2. Background thread is created via `threading.Thread` running its own `asyncio.new_event_loop()`
3. GUI's `shutdown_callback` uses `loop.call_soon_threadsafe(shutdown_event.set)` to signal the background loop
4. GUI runs `tkinter.mainloop()` on the main thread; background asyncio loop runs all I/O

**State Management:**
- `FileListener._stats` dict tracks scanned/uploaded/skipped/error counts (in-memory, reset on restart)
- `.milo_backup.info` files persist backup state to disk per directory
- `FolderWatcher._stats` tracks watched folder count and event counts (in-memory)

## Key Abstractions

**SimpleConfig:**
- Purpose: Single source of truth for all settings; passed by reference to all components
- Examples: `aws_copier/models/simple_config.py`
- Pattern: Plain class with `**kwargs` constructor; loaded from `config.yaml` via `load_from_yaml()`; supports both list and dict format for `watch_folders`

**FileListener:**
- Purpose: Central orchestrator for the incremental backup algorithm
- Examples: `aws_copier/core/file_listener.py`
- Pattern: All public methods are `async`; internal methods prefixed with `_`; folder-level granularity for backup tracking

**S3Manager:**
- Purpose: Encapsulates all AWS SDK calls; manages client lifecycle
- Examples: `aws_copier/core/s3_manager.py`
- Pattern: Single persistent client via `AsyncExitStack`; supports small files (`put_object`) and large files >100MB (`multipart_upload`); MD5 stored in S3 object metadata as `md5-checksum`

**FileChangeHandler:**
- Purpose: Bridges synchronous watchdog events to the async event loop
- Examples: `aws_copier/core/folder_watcher.py`
- Pattern: Inherits from `watchdog.events.FileSystemEventHandler`; uses `call_soon_threadsafe` for thread safety

## Entry Points

**Headless CLI (`main.py`):**
- Location: `/Users/nimrodmilo/dev/private/aws-copier/main.py`
- Triggers: Direct `python main.py` or `aws-copier` script (via `pyproject.toml`)
- Responsibilities: Config check, component initialization, initial scan, start watcher, 5-minute status loop

**GUI Entry (`main_gui.py`):**
- Location: `/Users/nimrodmilo/dev/private/aws-copier/main_gui.py`
- Triggers: `python main_gui.py`
- Responsibilities: Same as headless, plus tkinter GUI on main thread with background asyncio loop

**Test GUI (`test_gui.py`):**
- Location: `/Users/nimrodmilo/dev/private/aws-copier/test_gui.py`
- Triggers: Direct invocation for GUI smoke testing

## Error Handling

**Strategy:** Log-and-continue; errors increment `_stats["errors"]`; no exceptions propagate to callers from core upload/scan paths

**Patterns:**
- `asyncio.wait_for(..., timeout=300)` wraps all individual file uploads (5 minute max)
- `asyncio.wait_for(..., timeout=30)` wraps S3 existence checks
- `PermissionError` caught at directory traversal level; processing continues for other directories
- S3 `ClientError` with code `404` handled explicitly in `check_exists` — returns `False` rather than raising
- Multipart upload failures call `abort_multipart_upload` before re-raising

## Cross-Cutting Concerns

**Logging:** Python stdlib `logging`; all modules use `logging.getLogger(__name__)`; GUI mode adds a `LogHandler` that captures to a `queue.Queue` for display in tkinter

**Validation:** Input validation at config load time (file existence checks on `watch_folders`); S3 bucket validated via `head_bucket` during `S3Manager.initialize()`

**Authentication:** AWS credentials stored in `config.yaml` (plaintext); passed directly to `aiobotocore` `create_client` calls; no IAM role / environment variable fallback in the current implementation

**Ignore Lists:** Both `FileListener` and `FileChangeHandler` maintain their own hardcoded ignore pattern sets (duplicated); covers `.DS_Store`, temp files, `.git`, `node_modules`, `__pycache__`, etc.

---

*Architecture analysis: 2026-04-24*
