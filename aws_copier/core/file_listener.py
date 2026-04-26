"""File listener for incremental backup with .milo_backup.info tracking."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiofiles

from aws_copier.core.ignore_rules import IGNORE_RULES
from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)


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

        # CONFIG-01: Semaphore wired to user-configured max_concurrent_uploads (default 100).
        self.upload_semaphore = asyncio.Semaphore(self.config.max_concurrent_uploads)

        # Separate semaphore for MD5 computation to avoid blocking uploads.
        self.md5_semaphore = asyncio.Semaphore(50)

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
        """Migrate old string backup-info entry to new {md5, mtime} dict format.

        D-01: Old entries (str MD5) are read as {md5: value, mtime: 0.0}. mtime=0.0
        guarantees the next scan re-stats and writes the new format on first hit.

        Args:
            value: Raw entry from .milo_backup.info (str for old format, dict for new)

        Returns:
            Dict with keys 'md5' (str) and 'mtime' (float)
        """
        if isinstance(value, str):
            return {"md5": value, "mtime": 0.0}
        return value

    async def scan_all_folders(self) -> None:
        """Scan all configured watch folders using incremental backup approach."""
        logger.info("Starting incremental backup scan of all watch folders")

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

            # Step 2: Process all subfolders recursively
            try:
                for item in folder_path.iterdir():
                    if item.is_dir() and not IGNORE_RULES.should_ignore_dir(item):
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

        # Step 2: Scan current files and compute MD5s
        current_files = await self._scan_current_files(folder_path)

        # Step 3: Compare with existing backup info
        files_to_upload = self._determine_files_to_upload(current_files, existing_backup_info)

        # Step 4: Upload changed/new files
        uploaded_files = await self._upload_files(files_to_upload, folder_path)

        # Step 5: Update backup info file with successfully uploaded files only
        if uploaded_files:
            # Create updated backup info with only successfully uploaded files
            updated_backup_info: Dict[str, Any] = existing_backup_info.copy()

            # Add/update entries for successfully uploaded files; normalise to new dict format
            for filename in uploaded_files:
                entry = current_files.get(filename)
                if entry is not None:
                    updated_backup_info[filename] = (
                        entry if isinstance(entry, dict) else {"md5": entry, "mtime": 0.0}
                    )

            # Also include unchanged files that weren't uploaded (they're still valid)
            for filename, entry in current_files.items():
                if filename not in files_to_upload:  # File wasn't changed, keep existing info
                    updated_backup_info[filename] = (
                        entry if isinstance(entry, dict) else {"md5": entry, "mtime": 0.0}
                    )

            await self._update_backup_info(backup_info_file, updated_backup_info)
            logger.info(
                f"Updated backup info for {folder_path}: {len(uploaded_files)} uploaded, {len(files_to_upload) - len(uploaded_files)} failed"
            )
        elif files_to_upload:
            # Some files needed upload but none succeeded
            logger.warning(f"No files uploaded successfully in {folder_path}, backup info not updated")
        else:
            # No files needed upload, but update backup info to include any new unchanged files.
            # Normalise current_files to new dict format before comparing/writing.
            normalised_current: Dict[str, Any] = {
                fname: (entry if isinstance(entry, dict) else {"md5": entry, "mtime": 0.0})
                for fname, entry in current_files.items()
            }
            if normalised_current != existing_backup_info:
                await self._update_backup_info(backup_info_file, normalised_current)
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
            data: Dict[str, Dict[str, Any]] = {
                name: self._migrate_entry(v) for name, v in raw.items()
            }
            self._backup_info_cache[folder] = data
            self._backup_info_mtime[folder] = disk_mtime
            return data

    async def _scan_current_files(self, folder_path: Path) -> Dict[str, str]:
        """Scan current folder and compute MD5 for all files in parallel.

        Args:
            folder_path: Path to folder to scan

        Returns:
            Dictionary mapping filename to MD5 hash
        """
        current_files = {}

        try:
            # Collect all files to process
            files_to_scan = []
            for file_path in folder_path.iterdir():
                if file_path.is_dir():
                    continue
                # IGNORE-03: delegate to IGNORE_RULES; IGNORE-04: count ignored files in stats
                if IGNORE_RULES.should_ignore_file(file_path):
                    self._stats["ignored_files"] += 1
                    continue
                files_to_scan.append(file_path)

            if not files_to_scan:
                return current_files

            # Create tasks for parallel MD5 computation
            md5_tasks = []
            for file_path in files_to_scan:
                task = asyncio.create_task(self._calculate_md5_with_semaphore(file_path))
                md5_tasks.append((file_path.name, task))

            logger.debug(f"Computing MD5 for {len(files_to_scan)} files in parallel (max 50 concurrent)")

            # Gather all MD5 tasks concurrently and validate the responses
            results = await asyncio.gather(*(task for _, task in md5_tasks), return_exceptions=True)
            for (filename, _), result in zip(md5_tasks, results):
                if isinstance(result, Exception):
                    logger.error(f"Error computing MD5 for {filename}: {result}")
                    self._stats["errors"] += 1
                elif result:
                    current_files[filename] = result
                    self._stats["scanned_files"] += 1
                else:
                    self._stats["errors"] += 1

        except Exception as e:
            logger.error(f"Error scanning files in {folder_path}: {e}")

        return current_files

    def _determine_files_to_upload(
        self, current_files: Dict[str, Any], existing_backup_info: Dict[str, Any]
    ) -> List[str]:
        """Determine which files need to be uploaded.

        Handles both the legacy string format and the new {md5, mtime} dict format
        in existing_backup_info — extracts the md5 key when the entry is a dict.

        Args:
            current_files: Current files mapping filename to MD5 string or {md5, mtime} dict
            existing_backup_info: Existing backup info mapping filename to MD5 string or {md5, mtime} dict

        Returns:
            List of filenames that need to be uploaded
        """
        files_to_upload = []

        for filename, current_entry in current_files.items():
            # Support both bare MD5 strings (pre-Task-2) and new {md5, mtime} dicts
            current_md5 = current_entry if isinstance(current_entry, str) else current_entry.get("md5")
            existing_entry = existing_backup_info.get(filename)
            existing_md5 = existing_entry if isinstance(existing_entry, str) else (
                existing_entry.get("md5") if isinstance(existing_entry, dict) else None
            )

            if existing_md5 != current_md5:
                # File is new or has changed
                files_to_upload.append(filename)
            else:
                # File unchanged
                self._stats["skipped_files"] += 1

        return files_to_upload

    async def _upload_single_file(self, filename: str, folder_path: Path) -> bool:
        """Upload a single file with semaphore control.

        Args:
            filename: Name of file to upload
            folder_path: Path to folder containing the files

        Returns:
            True if successfully uploaded, False otherwise
        """
        async with self.upload_semaphore:
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
                    return False

                # Check if file exists in S3 with same MD5
                if await self.s3_manager.check_exists(s3_key, local_md5):
                    logger.info(f"File already exists in S3 with same MD5: {s3_key}")
                    self._stats["skipped_files"] += 1
                    return True

                # Upload file
                if await self.s3_manager.upload_file(file_path, s3_key):
                    self._stats["uploaded_files"] += 1
                    logger.info(f"Uploaded: {file_path} -> {s3_key}")
                    return True
                else:
                    logger.error(f"Failed to upload: {file_path}")
                    self._stats["errors"] += 1
                    return False

            except Exception as e:
                logger.error(f"Error uploading {file_path}: {e}")
                self._stats["errors"] += 1
                return False

    async def _upload_with_timeout(self, filename: str, folder_path: Path) -> Tuple[str, bool]:
        """Wrap a single upload in asyncio.wait_for so the 300s window belongs to the coroutine, not the task.

        ASYNC-02 Pitfall 1 fix: when wait_for was applied to an already-created task
        inside a serial for-loop, the N-th file's timeout window began only after the
        (N-1)-th completed or timed out. Wrapping the coroutine BEFORE create_task ensures
        each file gets its own independent 300s window running concurrently.

        Args:
            filename: Name of file to upload
            folder_path: Folder containing the file

        Returns:
            Tuple (filename, success_bool). Exceptions are caught and surfaced as (filename, False).
        """
        try:
            result = await asyncio.wait_for(self._upload_single_file(filename, folder_path), timeout=300)
            return filename, bool(result)
        except asyncio.TimeoutError:
            logger.error(f"Upload timeout for {filename} (5 minutes)")
            self._stats["errors"] += 1
            return filename, False
        except Exception as e:
            logger.error(f"Upload task failed for {filename}: {e}")
            self._stats["errors"] += 1
            return filename, False

    async def _upload_files(self, files_to_upload: List[str], folder_path: Path) -> List[str]:
        """Upload files to S3 concurrently using asyncio.gather.

        ASYNC-02 fix: replaces serial-wait_for-in-for-loop with a single gather call so all
        N uploads race their 300s timeouts concurrently. ASYNC-06 hook: each task is also
        added to self._active_upload_tasks so the shutdown drain can await in-flight work.

        Args:
            files_to_upload: List of filenames to upload
            folder_path: Folder containing the files

        Returns:
            List of filenames that were successfully uploaded
        """
        if not files_to_upload:
            return []

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
        # failure from cancelling the rest.
        results = await asyncio.gather(*tasks, return_exceptions=True)

        uploaded_files: List[str] = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Upload coroutine raised unexpectedly: {result}")
                self._stats["errors"] += 1
                continue
            filename, ok = result
            if ok:
                uploaded_files.append(filename)
                logger.debug(f"Successfully uploaded: {filename}")
            else:
                logger.warning(f"Upload failed for: {filename}")

        logger.info(f"Completed concurrent upload: {len(uploaded_files)} / {len(files_to_upload)} files uploaded")
        return uploaded_files

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

    async def _calculate_md5(self, file_path: Path) -> Optional[str]:
        """Calculate MD5 hash of a file using aiofiles for truly async I/O.

        Args:
            file_path: Path to file

        Returns:
            MD5 hash as hex string, or None if error
        """
        try:
            hasher = hashlib.md5()

            # Use aiofiles for truly asynchronous file I/O
            async with aiofiles.open(file_path, "rb") as f:
                while chunk := await f.read(8192):
                    hasher.update(chunk)

            return hasher.hexdigest()

        except Exception as e:
            logger.error(f"Error calculating MD5 for {file_path}: {e}")
            return None

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
            "skipped_files": 0,
            "errors": 0,
        }
