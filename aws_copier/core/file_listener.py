"""File listener for incremental backup with .milo_backup.info tracking."""

import asyncio
import hashlib
import json
import logging
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiofiles
from pathspec import PathSpec
from tqdm import tqdm as sync_tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from aws_copier.core.ignore_rules import IGNORE_RULES
from aws_copier.core.s3_manager import S3Manager, estimate_upload_timeout
from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)

# DOCKER-01: tqdm's carriage-return progress redraws render as garbled, duplicated lines
# through `docker logs` (no TTY — every redraw becomes its own log line instead of
# overwriting in place). Disabled whenever stdout isn't a real terminal; the web dashboard
# (aws_copier/web/dashboard.py) is the progress UI for that case instead.
_TQDM_DISABLE = not sys.stdout.isatty()


class FileListener:
    """Incremental backup scanner with .milo_backup.info tracking."""

    def __init__(self, config: SimpleConfig, s3_manager: S3Manager):
        """Initialize file listener.

        Args:
            config: Application configuration
            s3_manager: S3Manager instance for uploading files (required)
        """
        self.config = config
        self.s3_manager = s3_manager
        self.backup_info_filename = ".milo_backup.info"

        # CONFIG-01: Semaphore wired to user-configured max_concurrent_uploads (default 10).
        self.upload_semaphore = asyncio.Semaphore(self.config.max_concurrent_uploads)

        # Separate semaphore for MD5 computation to avoid blocking uploads.
        self.md5_semaphore = asyncio.Semaphore(10)

        # ASYNC-03: per-folder asyncio.Lock registry. Protects read-modify-write on
        # .milo_backup.info against concurrent scan + real-time event hitting the same folder.
        self._folder_locks: Dict[Path, asyncio.Lock] = {}

        # PERF-02: in-memory backup info cache; keyed by Path (same type as _folder_locks).
        # Mutations are guarded by the same per-folder lock as _load_backup_info /
        # _update_backup_info, so reads/writes are serialised per folder.
        self._backup_info_cache: Dict[Path, Dict[str, Any]] = {}
        self._backup_info_mtime: Dict[Path, float] = {}

        # ASYNC-06 hook: active upload tasks tracked here so the shutdown drain can wait on them.
        # Tasks add themselves in _upload_files; done-callback discards them.
        self._active_upload_tasks: Set[asyncio.Task] = set()

        # ASYNC-04: do not call asyncio.get_event_loop() inside this class — use
        # asyncio.get_running_loop() or asyncio.to_thread() when a loop reference is needed.

        # Statistics
        self._stats = {
            "scanned_folders": 0,
            "scanned_files": 0,
            "ignored_files": 0,
            "uploaded_files": 0,
            "moved_files": 0,
            "skipped_files": 0,
            "errors": 0,
        }

    def _get_folder_lock(self, folder_path: Path) -> asyncio.Lock:
        """Return (creating if needed) the asyncio.Lock guarding backup-info I/O for a folder.

        Args:
            folder_path: Directory whose .milo_backup.info file needs serialised access

        Returns:
            asyncio.Lock unique to this folder path (same instance across repeat calls)
        """
        if folder_path not in self._folder_locks:
            self._folder_locks[folder_path] = asyncio.Lock()
        return self._folder_locks[folder_path]

    def _migrate_entry(self, value: Any) -> Dict[str, Any]:
        """Migrate old backup-info entry formats to the current {md5, mtime, s3_key} shape.

        D-01: Old string-only entries (bare MD5) are read as {md5: value, mtime: 0.0,
        s3_key: None}. mtime=0.0 guarantees the next scan re-stats and writes the new
        format on first hit.

        MOVE-01: Entries from before the s3_key field existed are missing that key
        entirely; they're backfilled with s3_key=None. None is a deliberate "unknown"
        marker (not the folder-rename-detection sentinel) — _determine_files_to_upload
        treats it as "trust this entry", so a pre-existing correctly-synced file isn't
        forced through a spurious move on first scan after upgrade. Only a *known* stale
        s3_key (recorded before a subsequent rename) triggers move detection.

        Args:
            value: Raw entry from .milo_backup.info (str, or dict missing/containing s3_key)

        Returns:
            Dict with keys 'md5' (str), 'mtime' (float), and 's3_key' (str or None)
        """
        if isinstance(value, str):
            return {"md5": value, "mtime": 0.0, "s3_key": None}
        if "s3_key" not in value:
            return {**value, "s3_key": None}
        return value

    def _resolve_watch_root(self, folder_path: Path) -> Path:
        """Find the configured watch folder that is an ancestor of folder_path.

        Args:
            folder_path: Directory being scanned

        Returns:
            The watch root Path that contains folder_path, or folder_path itself
            as a fallback when no watch folder is an ancestor.
        """
        for watch_folder in self.config.watch_folders:
            try:
                folder_path.relative_to(watch_folder)
                return watch_folder
            except ValueError:
                continue
        # Fallback: treat folder_path itself as root (no ancestor cascade)
        return folder_path

    def _load_backupignore_spec(self, folder_path: Path, watch_root: Path) -> PathSpec:
        """Accumulate .backupignore patterns from watch_root down to folder_path.

        D-07: Patterns cascade into subdirectories — a rule in /photos/.backupignore
              applies to /photos/2024/ as well.
        D-08: Child directory rules ADD to (not replace) ancestor rules.
              Evaluated in path order: root → parent → child.
        Pitfall 4: Caller must pass the path relative to watch_root (with forward
                   slashes) to spec.match_file; absolute paths silently fail.

        Args:
            folder_path: Current directory being scanned
            watch_root: Root watch folder (ancestor boundary)

        Returns:
            PathSpec compiled from all applicable .backupignore files. If none
            exist or all reads fail, the returned spec matches no files.
        """
        all_patterns: List[str] = []
        try:
            parts = folder_path.relative_to(watch_root).parts
        except ValueError:
            parts = ()
        current = watch_root
        # Walk root → ... → folder_path, collecting .backupignore files in path order
        for part in ("",) + parts:
            if part:
                current = current / part
            ignore_file = current / ".backupignore"
            if ignore_file.exists():
                try:
                    lines = ignore_file.read_text(encoding="utf-8").splitlines()
                    all_patterns.extend(lines)
                except Exception as e:
                    logger.warning(f"Could not read {ignore_file}: {e}")
        return PathSpec.from_lines("gitignore", all_patterns)

    async def scan_all_folders(self) -> None:
        """Scan all configured watch folders using incremental backup approach."""
        logger.info("Starting incremental backup scan of all watch folders")

        with logging_redirect_tqdm():
            for folder_path in self.config.watch_folders:
                if not folder_path.exists():
                    logger.warning(f"Watch folder does not exist: {folder_path}")
                    continue

                if not folder_path.is_dir():
                    logger.warning(f"Watch path is not a directory: {folder_path}")
                    continue

                logger.info(f"Processing folder: {folder_path}")
                await self._process_folder_recursively(folder_path)

        logger.info(f"Incremental backup completed. Stats: {self._stats}")

    async def _process_folder_recursively(self, folder_path: Path) -> None:
        """Process a folder and all its subfolders recursively.

        Args:
            folder_path: Path to folder to process
        """
        try:
            # Skip ignored directories (IGNORE-03: delegate to IGNORE_RULES)
            if IGNORE_RULES.should_ignore_dir(folder_path):
                return

            logger.info(f"Processing folder: {folder_path}")
            self._stats["scanned_folders"] += 1

            # Step 1: Process current folder
            await self._process_current_folder(folder_path)

            # Step 2: Process all subfolders recursively in random order so each run
            # covers a different slice of the tree first, preventing starvation of deep dirs.
            try:
                subdirs = [
                    item for item in folder_path.iterdir() if item.is_dir() and not IGNORE_RULES.should_ignore_dir(item)
                ]
                random.shuffle(subdirs)
                for item in subdirs:
                    await self._process_folder_recursively(item)
            except PermissionError:
                logger.warning(f"Permission denied accessing folder: {folder_path}")
            except Exception as e:
                logger.error(f"Error scanning subfolders in {folder_path}: {e}")

        except Exception as e:
            logger.error(f"Error processing folder {folder_path}: {e}")
            self._stats["errors"] += 1

    async def _process_current_folder(self, folder_path: Path) -> None:
        """Process files in current folder using incremental backup logic.

        Args:
            folder_path: Path to folder to process
        """
        backup_info_file = folder_path / self.backup_info_filename

        # Step 1: Load existing backup info
        existing_backup_info = await self._load_backup_info(backup_info_file)

        # Step 2: Scan current files — pass existing_backup_info for PERF-01 mtime-skip
        watch_root = self._resolve_watch_root(folder_path)
        current_files = await self._scan_current_files(folder_path, existing_backup_info, watch_root)

        # Step 3: Compare with existing backup info — MOVE-01 splits changed content
        # (files_to_upload) from unchanged content at a stale S3 key (files_to_move).
        files_to_upload, files_to_move = self._determine_files_to_upload(
            current_files, existing_backup_info, folder_path
        )

        # Step 4: Upload changed/new files; receive {filename: upload_mtime} for successful uploads
        upload_mtimes = await self._upload_files(files_to_upload, folder_path)
        uploaded_files = list(upload_mtimes.keys())

        # Step 4b (MOVE-01): reconcile renamed/moved files via server-side S3 copy
        moved_files = await self._move_files(files_to_move, existing_backup_info, folder_path)

        # Step 5: Update backup info file with successfully uploaded/moved files
        if uploaded_files or moved_files:
            # Create updated backup info with only successfully uploaded/moved files
            updated_backup_info: Dict[str, Any] = existing_backup_info.copy()

            # Add/update entries for successfully uploaded files with upload-captured mtime (D-02)
            for filename in uploaded_files:
                entry = current_files.get(filename)
                if entry is not None:
                    current_md5 = entry.get("md5") if isinstance(entry, dict) else entry
                    updated_backup_info[filename] = {
                        "md5": current_md5,
                        "mtime": upload_mtimes[filename],
                        "s3_key": self._build_s3_key(folder_path / filename),
                    }

            # MOVE-01: record the new (current) s3_key for successfully moved files — content
            # and mtime are unchanged, only the S3 location was reconciled.
            for filename in moved_files:
                entry = current_files.get(filename)
                if entry is not None:
                    updated_backup_info[filename] = {
                        "md5": entry.get("md5") if isinstance(entry, dict) else entry,
                        "mtime": entry.get("mtime") if isinstance(entry, dict) else 0.0,
                        "s3_key": self._build_s3_key(folder_path / filename),
                    }

            # Also include unchanged files that weren't uploaded or moved (still valid) —
            # backfill s3_key so legacy/untracked entries pick it up going forward.
            handled = set(uploaded_files) | set(moved_files)
            for filename, entry in current_files.items():
                if filename in files_to_upload or filename in files_to_move:
                    continue  # covered above (or failed — leave existing_backup_info's copy)
                if filename in handled:
                    continue
                current_md5 = entry.get("md5") if isinstance(entry, dict) else entry
                current_mtime = entry.get("mtime") if isinstance(entry, dict) else 0.0
                updated_backup_info[filename] = {
                    "md5": current_md5,
                    "mtime": current_mtime,
                    "s3_key": self._build_s3_key(folder_path / filename),
                }

            await self._update_backup_info(backup_info_file, updated_backup_info)
            logger.info(
                f"Updated backup info for {folder_path}: {len(uploaded_files)} uploaded, "
                f"{len(moved_files)} moved, {len(files_to_upload) - len(uploaded_files)} upload-failed, "
                f"{len(files_to_move) - len(moved_files)} move-failed"
            )
        elif files_to_upload or files_to_move:
            # Some files needed upload/move but none succeeded
            logger.warning(f"No files uploaded or moved successfully in {folder_path}, backup info not updated")
        else:
            # No files needed upload or move, but update backup info to include any new
            # unchanged files and backfill s3_key on legacy entries.
            backfilled: Dict[str, Any] = {
                filename: {
                    "md5": entry.get("md5") if isinstance(entry, dict) else entry,
                    "mtime": entry.get("mtime") if isinstance(entry, dict) else 0.0,
                    "s3_key": self._build_s3_key(folder_path / filename),
                }
                for filename, entry in current_files.items()
            }
            if backfilled != existing_backup_info:
                await self._update_backup_info(backup_info_file, backfilled)
                logger.info(f"Updated backup info for {folder_path} with unchanged files")

    async def _load_backup_info(self, backup_info_file: Path) -> Dict[str, Dict[str, Any]]:
        """Load backup info with in-memory cache and silent format migration.

        PERF-02: Re-reads disk only when .milo_backup.info st_mtime changes.
        PERF-01 / D-01: Migrates old string entries to {md5, mtime} dict on read.
        ASYNC-03: Holds per-folder Lock during stat + read + cache update.

        Args:
            backup_info_file: Path to .milo_backup.info file

        Returns:
            Dict mapping filename to {"md5": str, "mtime": float}. Empty dict if
            the file does not exist or cannot be parsed.
        """
        folder = backup_info_file.parent
        async with self._get_folder_lock(folder):
            try:
                disk_mtime = backup_info_file.stat().st_mtime
            except FileNotFoundError:
                return {}
            # PERF-02: cache hit
            if self._backup_info_mtime.get(folder) == disk_mtime:
                return self._backup_info_cache.get(folder, {})
            # cache miss: read from disk
            try:
                async with aiofiles.open(backup_info_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                raw = json.loads(content).get("files", {})
            except Exception as e:
                logger.warning(f"Failed to load backup info from {backup_info_file}: {e}")
                return {}
            # PERF-01 / D-01: migrate entries
            data: Dict[str, Dict[str, Any]] = {name: self._migrate_entry(v) for name, v in raw.items()}
            self._backup_info_cache[folder] = data
            self._backup_info_mtime[folder] = disk_mtime
            return data

    async def _scan_current_files(
        self,
        folder_path: Path,
        existing_backup_info: Optional[Dict[str, Dict[str, Any]]] = None,
        watch_root: Optional[Path] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Scan current folder and compute MD5 for changed files; skip unchanged via mtime (PERF-01).

        Args:
            folder_path: Path to folder to scan
            existing_backup_info: Previously stored backup info dict (from _load_backup_info).
                When provided, files whose st_mtime matches the stored mtime are skipped —
                MD5 is not recomputed. Pass None or {} to force full recomputation.
            watch_root: The ancestor watch root for this folder_path; used to build the
                cascaded .backupignore PathSpec (CONFIG-06). Defaults to folder_path
                itself when not provided (no ancestor cascade).

        Returns:
            Dictionary mapping filename to {"md5": str, "mtime": float}
        """
        if existing_backup_info is None:
            existing_backup_info = {}
        if watch_root is None:
            watch_root = folder_path

        # CONFIG-06: build cascaded .backupignore spec for this folder (D-07, D-08)
        backupignore_spec = self._load_backupignore_spec(folder_path, watch_root)

        current_files: Dict[str, Dict[str, Any]] = {}

        try:
            # Collect all files to process; apply mtime-skip before scheduling MD5 tasks
            files_to_hash: List[Path] = []
            for file_path in folder_path.iterdir():
                if file_path.is_dir():
                    continue
                # IGNORE-03: delegate to IGNORE_RULES; IGNORE-04: count ignored files in stats
                if IGNORE_RULES.should_ignore_file(file_path):
                    self._stats["ignored_files"] += 1
                    continue

                # CONFIG-06: per-directory .backupignore filtering. Match input must be the
                # path relative to watch_root with forward slashes (Pitfall 4).
                try:
                    relative = file_path.relative_to(watch_root)
                except ValueError:
                    relative = Path(file_path.name)
                relative_str = str(relative).replace("\\", "/")
                if backupignore_spec.match_file(relative_str):
                    self._stats["ignored_files"] += 1
                    continue

                # PERF-01: check st_mtime before scheduling MD5 computation
                try:
                    stat = file_path.stat()
                except OSError:
                    # Cannot stat — fall through to MD5 computation
                    files_to_hash.append(file_path)
                    continue

                entry = existing_backup_info.get(file_path.name)
                if entry and entry.get("mtime") == stat.st_mtime:
                    # mtime unchanged → skip MD5, carry forward existing entry
                    self._stats["skipped_files"] += 1
                    current_files[file_path.name] = entry
                    continue

                files_to_hash.append(file_path)

            if not files_to_hash:
                return current_files

            # Create tasks for parallel MD5 computation on files that need it
            md5_tasks = []
            for file_path in files_to_hash:
                task = asyncio.create_task(self._calculate_md5_with_semaphore(file_path))
                md5_tasks.append((file_path, task))

            logger.debug(f"Computing MD5 for {len(files_to_hash)} files in parallel (max 50 concurrent)")

            # Gather all MD5 tasks concurrently; done callbacks tick the progress bar.
            pbar = sync_tqdm(
                total=len(md5_tasks),
                desc=f"Hashing  {folder_path.name}",
                unit="file",
                leave=False,
                disable=_TQDM_DISABLE,
            )
            for _, task in md5_tasks:
                task.add_done_callback(lambda _f: pbar.update())
            results = await asyncio.gather(*(task for _, task in md5_tasks), return_exceptions=True)
            pbar.close()
            for (file_path, _), result in zip(md5_tasks, results):
                if isinstance(result, Exception):
                    logger.error(f"Error computing MD5 for {file_path.name}: {result}")
                    self._stats["errors"] += 1
                elif result:
                    # Store new dict format; mtime captured at scan time (D-02 note: upload
                    # will re-capture mtime just before the upload call for uploaded files)
                    try:
                        scan_mtime = file_path.stat().st_mtime
                    except OSError:
                        scan_mtime = 0.0
                    current_files[file_path.name] = {"md5": result, "mtime": scan_mtime}
                    self._stats["scanned_files"] += 1
                else:
                    self._stats["errors"] += 1

        except Exception as e:
            logger.error(f"Error scanning files in {folder_path}: {e}")

        return current_files

    def _determine_files_to_upload(
        self, current_files: Dict[str, Any], existing_backup_info: Dict[str, Any], folder_path: Path
    ) -> Tuple[List[str], List[str]]:
        """Determine which files need uploading, and which merely need an S3-side move.

        MOVE-01: A file whose content is unchanged (MD5 matches) but whose currently
        computed S3 key differs from the key recorded when it was last synced means the
        directory it lives in was renamed/moved on disk — `.milo_backup.info` travels
        with the directory, so the stale s3_key is exactly the evidence needed to detect
        this. Such files go to files_to_move (server-side S3 copy, no re-upload) rather
        than files_to_upload. A None recorded s3_key (legacy entry, see _migrate_entry)
        is trusted as-is and never triggers a move.

        Handles both the legacy string format and the {md5, mtime, s3_key} dict format
        in existing_backup_info — extracts the md5/s3_key keys when the entry is a dict.

        Args:
            current_files: Current files mapping filename to MD5 string or {md5, mtime} dict
            existing_backup_info: Existing backup info mapping filename to MD5 string or
                {md5, mtime, s3_key} dict
            folder_path: Folder containing these files, used to compute each file's current
                S3 key for the move-detection comparison

        Returns:
            Tuple of (files_to_upload, files_to_move) filename lists.
        """
        files_to_upload = []
        files_to_move = []

        for filename, current_entry in current_files.items():
            # Support both bare MD5 strings (pre-Task-2) and new {md5, mtime} dicts
            current_md5 = current_entry if isinstance(current_entry, str) else current_entry.get("md5")
            existing_entry = existing_backup_info.get(filename)
            existing_md5 = (
                existing_entry
                if isinstance(existing_entry, str)
                else (existing_entry.get("md5") if isinstance(existing_entry, dict) else None)
            )

            if existing_md5 != current_md5:
                # File is new or has changed
                files_to_upload.append(filename)
                continue

            # MOVE-01: content unchanged — check whether its recorded S3 key is stale.
            existing_s3_key = existing_entry.get("s3_key") if isinstance(existing_entry, dict) else None
            if existing_s3_key is not None:
                current_s3_key = self._build_s3_key(folder_path / filename)
                if existing_s3_key != current_s3_key:
                    files_to_move.append(filename)
                    continue

            # File unchanged, no location drift
            self._stats["skipped_files"] += 1

        return files_to_upload, files_to_move

    async def _upload_single_file(self, filename: str, folder_path: Path) -> Tuple[bool, float]:
        """Upload a single file, capturing st_mtime just before upload (D-02).

        CONC-01: does NOT acquire upload_semaphore itself — the caller (_upload_with_timeout)
        holds it for exactly the duration of this call, so time spent queued waiting for a
        free slot is never charged against this file's timeout window (see CONC-01 note on
        _upload_with_timeout for why that distinction matters).

        Args:
            filename: Name of file to upload
            folder_path: Path to folder containing the files

        Returns:
            Tuple of (success: bool, upload_mtime: float). upload_mtime is the st_mtime
            captured immediately before the upload call, so a file modified during upload
            is detected on the next scan cycle. Returns (False, 0.0) on any failure.
        """
        file_path = folder_path / filename

        logger.info(f"Uploading file: {file_path}")
        try:
            # Build S3 key relative to watch folder root
            s3_key = self._build_s3_key(file_path)

            # Calculate local MD5
            local_md5 = await self._calculate_md5(file_path)
            if not local_md5:
                logger.error(f"Failed to calculate MD5 for {file_path}")
                self._stats["errors"] += 1
                return False, 0.0

            # Check if file exists in S3 with same MD5
            if await self.s3_manager.check_exists(s3_key, local_md5):
                logger.info(f"File already exists in S3 with same MD5: {s3_key}")
                self._stats["skipped_files"] += 1
                # Capture mtime for already-synced file so it can be recorded
                try:
                    upload_mtime = file_path.stat().st_mtime
                except OSError:
                    upload_mtime = 0.0
                return True, upload_mtime

            # D-02: capture st_mtime just before upload so a file modified during upload
            # is detected on the next cycle (we record what we actually uploaded).
            try:
                upload_mtime = file_path.stat().st_mtime
            except OSError:
                upload_mtime = 0.0

            # Upload file
            if await self.s3_manager.upload_file(file_path, s3_key, precomputed_md5=local_md5):
                self._stats["uploaded_files"] += 1
                logger.info(f"Uploaded: {file_path} -> {s3_key}")
                return True, upload_mtime
            else:
                logger.error(f"Failed to upload: {file_path}")
                self._stats["errors"] += 1
                return False, 0.0

        except Exception as e:
            logger.error(f"Error uploading {file_path}: {e}")
            self._stats["errors"] += 1
            return False, 0.0

    async def _upload_with_timeout(self, filename: str, folder_path: Path) -> Tuple[str, bool, float]:
        """Acquire the upload semaphore, then bound the upload itself in asyncio.wait_for.

        ASYNC-02 Pitfall 1 fix: when wait_for was applied to an already-created task
        inside a serial for-loop, the N-th file's timeout window began only after the
        (N-1)-th completed or timed out. Wrapping the coroutine BEFORE create_task ensures
        each file gets its own independent timeout window running concurrently.

        CONC-01: the semaphore is acquired BEFORE the timed region starts, not inside it.
        With hundreds of small files queued behind a few large/slow uploads (all sharing one
        semaphore), a file blocked waiting for a free slot was previously burning down its
        own timeout window while doing nothing — once enough large uploads occupied every
        slot for a while, every queued file would then time out in the same instant the
        moment their (uncounted-down-from-zero) window elapsed, even though none of them had
        actually started transferring. The timeout must bound the transfer, not the queue.

        PERF-05: the window scales with this file's size (estimate_upload_timeout) instead
        of a fixed 300s. This is the binding bound for large (>100MB, multipart) files —
        S3Manager only times out individual 5MB parts, not the multipart upload as a whole
        — so a large video file on a slow connection needs this outer window sized to its
        total bytes, not just S3Manager's per-part window.

        Args:
            filename: Name of file to upload
            folder_path: Folder containing the file

        Returns:
            Tuple (filename, success_bool, upload_mtime). upload_mtime is the st_mtime
            captured just before upload (D-02). On failure, upload_mtime is 0.0.
        """
        try:
            file_size = (folder_path / filename).stat().st_size
        except OSError:
            file_size = 0
        timeout = estimate_upload_timeout(file_size)
        async with self.upload_semaphore:
            try:
                ok, upload_mtime = await asyncio.wait_for(
                    self._upload_single_file(filename, folder_path), timeout=timeout
                )
                return filename, bool(ok), upload_mtime
            except asyncio.TimeoutError:
                logger.error(f"Upload timeout for {filename} ({timeout}s, {file_size / (1024 * 1024):.1f}MB)")
                self._stats["errors"] += 1
                return filename, False, 0.0
            except Exception as e:
                logger.error(f"Upload task failed for {filename}: {e}")
                self._stats["errors"] += 1
                return filename, False, 0.0

    async def _upload_files(self, files_to_upload: List[str], folder_path: Path) -> Dict[str, float]:
        """Upload files to S3 concurrently using asyncio.gather.

        ASYNC-02 fix: replaces serial-wait_for-in-for-loop with a single gather call so all
        N uploads race their 300s timeouts concurrently. ASYNC-06 hook: each task is also
        added to self._active_upload_tasks so the shutdown drain can await in-flight work.

        Args:
            files_to_upload: List of filenames to upload
            folder_path: Folder containing the files

        Returns:
            Dict mapping successfully-uploaded filename to the upload_mtime captured just
            before the upload call (D-02). Failed uploads are absent from the dict.
        """
        if not files_to_upload:
            return {}

        logger.info(
            f"Starting concurrent upload of {len(files_to_upload)} files "
            f"(max {self.config.max_concurrent_uploads} parallel)"
        )

        tasks: List[asyncio.Task] = []
        for filename in files_to_upload:
            task = asyncio.create_task(
                self._upload_with_timeout(filename, folder_path),
                name=f"upload-{folder_path.name}-{filename}",
            )
            self._active_upload_tasks.add(task)
            task.add_done_callback(self._active_upload_tasks.discard)
            tasks.append(task)

        # Gather all upload coroutines concurrently; return_exceptions=True prevents one
        # failure from cancelling the rest. Done callbacks tick the progress bar.
        pbar = sync_tqdm(
            total=len(tasks), desc=f"Uploading {folder_path.name}", unit="file", leave=True, disable=_TQDM_DISABLE
        )
        for task in tasks:
            task.add_done_callback(lambda _f: pbar.update())
        results = await asyncio.gather(*tasks, return_exceptions=True)
        pbar.close()

        uploaded_files: Dict[str, float] = {}
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Upload coroutine raised unexpectedly: {result}")
                self._stats["errors"] += 1
                continue
            filename, ok, upload_mtime = result
            if ok:
                uploaded_files[filename] = upload_mtime
                logger.debug(f"Successfully uploaded: {filename}")
            else:
                logger.warning(f"Upload failed for: {filename}")

        logger.info(f"Completed concurrent upload: {len(uploaded_files)} / {len(files_to_upload)} files uploaded")
        return uploaded_files

    async def _move_single_file(self, filename: str, old_s3_key: str, new_s3_key: str) -> bool:
        """Reconcile one renamed/moved file via server-side S3 copy, under the upload semaphore.

        MOVE-01: shares upload_semaphore with regular uploads since both are bounded S3 API
        work against the same bucket/rate limit, not separate resource pools.

        Args:
            filename: Name of file being reconciled (for logging only)
            old_s3_key: Stale key recorded in .milo_backup.info before the rename
            new_s3_key: Currently correct key for the file's present location

        Returns:
            True if the S3-side move succeeded, False otherwise.
        """
        async with self.upload_semaphore:
            try:
                ok = await self.s3_manager.move_object(old_s3_key, new_s3_key)
                if not ok:
                    logger.warning(f"Failed to move {filename}: {old_s3_key} -> {new_s3_key}")
                return ok
            except Exception as e:
                logger.error(f"Error moving {filename}: {e}")
                self._stats["errors"] += 1
                return False

    async def _move_with_timeout(self, filename: str, old_s3_key: str, new_s3_key: str) -> Tuple[str, bool]:
        """Wrap a single move in asyncio.wait_for so its 300s window runs independently (mirrors ASYNC-02).

        Args:
            filename: Name of file being reconciled
            old_s3_key: Stale key recorded in .milo_backup.info before the rename
            new_s3_key: Currently correct key for the file's present location

        Returns:
            Tuple (filename, success_bool).
        """
        try:
            ok = await asyncio.wait_for(self._move_single_file(filename, old_s3_key, new_s3_key), timeout=300)
            return filename, bool(ok)
        except asyncio.TimeoutError:
            logger.error(f"Move timeout for {filename} (5 minutes)")
            self._stats["errors"] += 1
            return filename, False
        except Exception as e:
            logger.error(f"Move task failed for {filename}: {e}")
            self._stats["errors"] += 1
            return filename, False

    async def _move_files(
        self, files_to_move: List[str], existing_backup_info: Dict[str, Any], folder_path: Path
    ) -> Set[str]:
        """Reconcile renamed/moved files concurrently via server-side S3 copy (MOVE-01).

        Args:
            files_to_move: Filenames whose content is unchanged but whose recorded S3 key
                is stale (from _determine_files_to_upload)
            existing_backup_info: Existing backup info, used to look up each file's stale
                (pre-rename) s3_key as the CopyObject source
            folder_path: Folder containing these files, used to compute each file's current
                (post-rename) S3 key as the CopyObject destination

        Returns:
            Set of filenames successfully moved. Filenames that fail stay unresolved —
            still orphaned at the old key and absent from the new key — and are retried
            on the next scan since their recorded s3_key remains stale.
        """
        if not files_to_move:
            return set()

        logger.info(f"Reconciling {len(files_to_move)} moved/renamed file(s) in {folder_path} via S3 server-side copy")

        tasks: List[asyncio.Task] = []
        for filename in files_to_move:
            existing_entry = existing_backup_info.get(filename)
            old_s3_key = existing_entry.get("s3_key") if isinstance(existing_entry, dict) else None
            if not old_s3_key:
                # Should not happen — files_to_move only contains entries with a known s3_key —
                # but skip defensively rather than call move_object with a bad source key.
                continue
            new_s3_key = self._build_s3_key(folder_path / filename)
            task = asyncio.create_task(
                self._move_with_timeout(filename, old_s3_key, new_s3_key),
                name=f"move-{folder_path.name}-{filename}",
            )
            self._active_upload_tasks.add(task)
            task.add_done_callback(self._active_upload_tasks.discard)
            tasks.append(task)

        if not tasks:
            return set()

        results = await asyncio.gather(*tasks, return_exceptions=True)

        moved_files: Set[str] = set()
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Move coroutine raised unexpectedly: {result}")
                self._stats["errors"] += 1
                continue
            filename, ok = result
            if ok:
                moved_files.add(filename)
                self._stats["moved_files"] += 1
                logger.info(f"Moved (S3 server-side copy): {filename}")
            else:
                logger.warning(f"Move failed for: {filename}")

        logger.info(f"Completed S3 move reconciliation: {len(moved_files)} / {len(files_to_move)} files moved")
        return moved_files

    def _build_s3_key(self, file_path: Path) -> str:
        """Build S3 key for a file path relative to watch folders.

        Args:
            file_path: Local file path

        Returns:
            S3 key string
        """
        # Find which watch folder this file belongs to
        for watch_folder in self.config.watch_folders:
            try:
                # Get relative path from watch folder
                relative_path = file_path.relative_to(watch_folder)
                # Use custom S3 name from mapping instead of folder name
                s3_folder_name = self.config.get_s3_name_for_folder(watch_folder)
                s3_key = f"{s3_folder_name}/{relative_path}"
                return s3_key.replace("\\", "/")  # Ensure forward slashes
            except ValueError:
                continue  # File not under this watch folder

        # Fallback: use absolute path (shouldn't happen normally)
        return str(file_path).replace("\\", "/")

    async def _update_backup_info(self, backup_info_file: Path, backup_files: Dict[str, Dict[str, Any]]) -> bool:
        """Update the .milo_backup.info file using aiofiles under the per-folder lock.

        ASYNC-03: async write via aiofiles; asyncio.Lock prevents a simultaneous read
        from seeing a half-written file.
        PERF-02: Updates in-memory cache after write and invalidates cached disk-mtime so
        the next _load_backup_info re-stats and picks up the new disk timestamp.

        Args:
            backup_info_file: Path to backup info file
            backup_files: Files and their {md5, mtime} dicts to record as backed up

        Returns:
            True if the write succeeded, False otherwise.
        """
        backup_info = {"timestamp": datetime.now().isoformat(), "files": backup_files}
        try:
            async with self._get_folder_lock(backup_info_file.parent):
                async with aiofiles.open(backup_info_file, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(backup_info, indent=2))
                # PERF-02: update cache with new content; invalidate cached disk mtime so the
                # next _load_backup_info re-stats and picks up the OS-assigned mtime after write.
                self._backup_info_cache[backup_info_file.parent] = backup_files
                self._backup_info_mtime.pop(backup_info_file.parent, None)
            return True
        except Exception as e:
            logger.error(f"Failed to update backup info {backup_info_file}: {e}")
            return False

    async def _calculate_md5_with_semaphore(self, file_path: Path) -> Optional[str]:
        """Calculate MD5 hash of a file with semaphore control for parallel processing.

        Args:
            file_path: Path to file

        Returns:
            MD5 hash as hex string, or None if error
        """
        async with self.md5_semaphore:
            return await self._calculate_md5(file_path)

    def _calculate_md5_sync(self, file_path: Path) -> Optional[str]:
        """Synchronous MD5 computation for use inside asyncio.to_thread.

        Args:
            file_path: Path to file

        Returns:
            MD5 hash as hex string, or None if error
        """
        try:
            hasher = hashlib.md5()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(1048576), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception as e:
            logger.error(f"Error calculating MD5 for {file_path}: {e}")
            return None

    async def _calculate_md5(self, file_path: Path) -> Optional[str]:
        """Calculate MD5 hash of a file by offloading to a thread pool.

        hashlib releases the GIL during C-level hashing, so multiple threads
        can genuinely parallelize. 1MB chunks minimise per-chunk overhead vs
        the old 8192-byte aiofiles loop.

        Args:
            file_path: Path to file

        Returns:
            MD5 hash as hex string, or None if error
        """
        return await asyncio.to_thread(self._calculate_md5_sync, file_path)

    def get_statistics(self) -> dict:
        """Get current statistics.

        Returns:
            Dictionary with current statistics
        """
        return dict(self._stats)

    def reset_statistics(self) -> None:
        """Reset statistics counters."""
        self._stats = {
            "scanned_folders": 0,
            "scanned_files": 0,
            "ignored_files": 0,
            "uploaded_files": 0,
            "moved_files": 0,
            "skipped_files": 0,
            "errors": 0,
        }

    def clear_caches(self) -> None:
        """Drop the in-memory backup-info cache (PERF-02) to bound memory over a long run.

        MEM-01: _backup_info_cache/_backup_info_mtime accumulate one entry per folder ever
        scanned or touched by a real-time event, for the lifetime of the process — for a
        large library (many thousands of directories) this is the main unbounded-growth
        point. Safe to clear at any time: the next _load_backup_info call for any folder
        just becomes a cache miss and re-reads that folder's .milo_backup.info from disk (a
        cheap stat + JSON parse), losing no correctness.

        _folder_locks is deliberately left untouched — Lock objects are tiny (not worth
        reclaiming), and removing one while a coroutine might still hold a reference to it
        could let two different Lock instances end up guarding the same folder concurrently.
        """
        self._backup_info_cache.clear()
        self._backup_info_mtime.clear()
