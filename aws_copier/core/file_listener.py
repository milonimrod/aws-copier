"""File listener for incremental backup with .milo_backup.info tracking."""

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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
        self.upload_semaphore = asyncio.Semaphore(100)

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
            "$RECYCLE.BIN",
            "System Volume Information",
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

            logger.debug(f"Processing folder: {folder_path}")
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

        # Step 5: Update backup info file if any files were uploaded
        if uploaded_files:
            await self._update_backup_info(backup_info_file, current_files)
            logger.info(f"Updated backup info for {folder_path} with {len(uploaded_files)} uploaded files")

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
        """Scan current folder and compute MD5 for all files.

        Args:
            folder_path: Path to folder to scan

        Returns:
            Dictionary mapping filename to MD5 hash
        """
        current_files = {}

        try:
            for file_path in folder_path.iterdir():
                # Skip directories and ignored files
                if file_path.is_dir() or self._should_ignore_file(file_path):
                    continue

                # Compute MD5 hash
                md5_hash = await self._calculate_md5(file_path)
                if md5_hash:
                    current_files[file_path.name] = md5_hash
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

        # Create tasks for all files
        upload_tasks = [self._upload_single_file(filename, folder_path) for filename in files_to_upload]

        # Run all uploads concurrently and gather results
        logger.info(f"Starting concurrent upload of {len(files_to_upload)} files (max 100 parallel)")
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)

        # Filter successful uploads
        uploaded_files = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Upload task failed with exception: {result}")
                self._stats["errors"] += 1
            elif isinstance(result, bool) and result:
                uploaded_files.append(result)
            else:
                logger.error(f"Upload task returned unexpected result: {result}")
                self._stats["errors"] += 1

        logger.info(
            f"Completed concurrent upload: {len(uploaded_files)} files uploaded successfully in {len(upload_tasks)} tasks"
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
                # Use watch folder name as prefix
                s3_key = f"{watch_folder.name}/{relative_path}"
                return s3_key.replace("\\", "/")  # Ensure forward slashes
            except ValueError:
                continue  # File not under this watch folder

        # Fallback: use absolute path (shouldn't happen normally)
        return str(file_path).replace("\\", "/")

    async def _update_backup_info(self, backup_info_file: Path, current_files: Dict[str, str]) -> None:
        """Update the .milo_backup.info file with current file states.

        Args:
            backup_info_file: Path to backup info file
            current_files: Current files and their MD5 hashes
        """
        backup_info = {"timestamp": datetime.now().isoformat(), "files": current_files}

        try:
            with open(backup_info_file, "w", encoding="utf-8") as f:
                json.dump(backup_info, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to update backup info {backup_info_file}: {e}")

    async def _calculate_md5(self, file_path: Path) -> Optional[str]:
        """Calculate MD5 hash of a file.

        Args:
            file_path: Path to file

        Returns:
            MD5 hash as hex string, or None if error
        """
        try:
            hasher = hashlib.md5()

            def _hash_file():
                with open(file_path, "rb") as f:
                    while chunk := f.read(8192):
                        hasher.update(chunk)
                return hasher.hexdigest()

            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _hash_file)

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
