"""File listener for incremental backup with .milo_backup.info tracking."""

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles

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

        # Semaphore to limit concurrent uploads
        self.upload_semaphore = asyncio.Semaphore(50)

        # Separate semaphore for MD5 computation to avoid blocking uploads
        self.md5_semaphore = asyncio.Semaphore(50)

        # File extensions and patterns to ignore
        self.ignore_patterns = {
            # System files (cross-platform)
            ".DS_Store",
            "Thumbs.db",
            "desktop.ini",
            # Windows system files
            "hiberfil.sys",
            "pagefile.sys",
            "swapfile.sys",
            # Temporary files
            ".tmp",
            ".temp",
            ".swp",
            ".swo",
            # Version control
            ".git",
            ".gitignore",
            ".svn",
            # IDE files
            ".vscode",
            ".idea",
            "*.pyc",
            "__pycache__",
            # OS specific (macOS)
            ".Trashes",
            ".Spotlight-V100",
            ".fseventsd",
            # Common build/cache directories
            "node_modules",
            ".pytest_cache",
            ".coverage",
            # Backup files
            "*.bak",
            "*.backup",
            "*~",
            # Our backup info file
            ".milo_backup.info",
        }

        # Directories to ignore completely
        self.ignore_dirs = {
            ".git",
            ".svn",
            ".hg",
            "__pycache__",
            ".pytest_cache",
            "node_modules",
            ".venv",
            "venv",
            ".aws-copier",  # Our own config folder
            # Windows system directories
            "$RECYCLE.BIN",
            "System Volume Information",
        }

        # Statistics
        self._stats = {
            "scanned_folders": 0,
            "scanned_files": 0,
            "ignored_files": 0,
            "uploaded_files": 0,
            "skipped_files": 0,
            "errors": 0,
        }

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
            # Skip ignored directories
            if self._should_ignore_directory(folder_path):
                return

            logger.info(f"Processing folder: {folder_path}")
            self._stats["scanned_folders"] += 1

            # Step 1: Process current folder
            await self._process_current_folder(folder_path)

            # Step 2: Process all subfolders recursively
            try:
                for item in folder_path.iterdir():
                    if item.is_dir() and not self._should_ignore_directory(item):
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
            updated_backup_info = existing_backup_info.copy()

            # Add/update entries for successfully uploaded files
            for filename in uploaded_files:
                if filename in current_files:
                    updated_backup_info[filename] = current_files[filename]

            # Also include unchanged files that weren't uploaded (they're still valid)
            for filename, md5_hash in current_files.items():
                if filename not in files_to_upload:  # File wasn't changed, keep existing info
                    updated_backup_info[filename] = md5_hash

            await self._update_backup_info(backup_info_file, updated_backup_info)
            logger.info(
                f"Updated backup info for {folder_path}: {len(uploaded_files)} uploaded, {len(files_to_upload) - len(uploaded_files)} failed"
            )
        elif files_to_upload:
            # Some files needed upload but none succeeded
            logger.warning(f"No files uploaded successfully in {folder_path}, backup info not updated")
        else:
            # No files needed upload, but update backup info to include any new unchanged files
            if current_files != existing_backup_info:
                await self._update_backup_info(backup_info_file, current_files)
                logger.info(f"Updated backup info for {folder_path} with unchanged files")

    async def _load_backup_info(self, backup_info_file: Path) -> Dict[str, str]:
        """Load existing backup info from .milo_backup.info file.

        Args:
            backup_info_file: Path to backup info file

        Returns:
            Dictionary mapping filename to MD5 hash
        """
        if not backup_info_file.exists():
            return {}

        try:
            with open(backup_info_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("files", {})
        except Exception as e:
            logger.warning(f"Failed to load backup info from {backup_info_file}: {e}")
            return {}

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
                # Skip directories and ignored files
                if file_path.is_dir() or self._should_ignore_file(file_path):
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
        self, current_files: Dict[str, str], existing_backup_info: Dict[str, str]
    ) -> List[str]:
        """Determine which files need to be uploaded.

        Args:
            current_files: Current files and their MD5 hashes
            existing_backup_info: Existing backup info with MD5 hashes

        Returns:
            List of filenames that need to be uploaded
        """
        files_to_upload = []

        for filename, current_md5 in current_files.items():
            existing_md5 = existing_backup_info.get(filename)

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

    async def _upload_files(self, files_to_upload: List[str], folder_path: Path) -> List[str]:
        """Upload files to S3 concurrently after checking remote MD5.

        Args:
            files_to_upload: List of filenames to upload
            folder_path: Path to folder containing the files

        Returns:
            List of filenames that were successfully uploaded
        """
        if not files_to_upload:
            return []

        # Create tasks for all files with timeout to prevent hanging
        upload_tasks = []
        for filename in files_to_upload:
            task = asyncio.create_task(self._upload_single_file(filename, folder_path))
            upload_tasks.append((filename, task))

        # Run all uploads concurrently with timeout
        logger.info(f"Starting concurrent upload of {len(files_to_upload)} files (max 50 parallel)")

        uploaded_files = []
        for filename, task in upload_tasks:
            try:
                # Add timeout to prevent hanging on individual files
                result = await asyncio.wait_for(task, timeout=300)  # 5 minute timeout per file

                if result:
                    uploaded_files.append(filename)
                    logger.debug(f"Successfully uploaded: {filename}")
                else:
                    logger.warning(f"Upload failed for: {filename}")
                    self._stats["errors"] += 1

            except asyncio.TimeoutError:
                logger.error(f"Upload timeout for {filename} (5 minutes)")
                self._stats["errors"] += 1
                task.cancel()  # Cancel the hanging task

            except Exception as e:
                logger.error(f"Upload task failed for {filename}: {e}")
                self._stats["errors"] += 1

        logger.info(
            f"Completed concurrent upload: {len(uploaded_files)} files uploaded successfully out of {len(files_to_upload)} total"
        )
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

    async def _update_backup_info(self, backup_info_file: Path, backup_files: Dict[str, str]) -> None:
        """Update the .milo_backup.info file with successfully backed up file states.

        Args:
            backup_info_file: Path to backup info file
            backup_files: Files and their MD5 hashes to record as backed up
        """
        backup_info = {"timestamp": datetime.now().isoformat(), "files": backup_files}

        try:
            with open(backup_info_file, "w", encoding="utf-8") as f:
                json.dump(backup_info, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to update backup info {backup_info_file}: {e}")

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

    def _should_ignore_file(self, file_path: Path) -> bool:
        """Check if a file should be ignored.

        Args:
            file_path: Path to file to check

        Returns:
            True if file should be ignored, False otherwise
        """
        filename = file_path.name

        # Check exact filename matches
        if filename in self.ignore_patterns:
            return True

        # Check if filename starts with patterns from ignore_patterns
        for pattern in self.ignore_patterns:
            if pattern.startswith(".") and filename.startswith(pattern):
                return True

        return False

    def _should_ignore_directory(self, dir_path: Path) -> bool:
        """Check if a directory should be ignored.

        Args:
            dir_path: Path to directory to check

        Returns:
            True if directory should be ignored, False otherwise
        """
        dirname = dir_path.name

        # Check if directory is in ignore list
        if dirname in self.ignore_dirs:
            return True

        # Check if directory starts with . (hidden directories)
        if dirname.startswith("."):
            return True

        try:
            if dir_path.is_symlink():
                return True
            if hasattr(dir_path, "is_dir") and hasattr(dir_path, "exists"):
                if dir_path.exists() and not dir_path.is_dir():
                    return True
        except Exception:
            return False
        return False

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
