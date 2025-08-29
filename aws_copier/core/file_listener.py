"""File listener for initial directory scanning."""

import asyncio
import logging
from pathlib import Path
from typing import List

from aws_copier.core.queue_manager import QueueManager
from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)


class FileListener:
    """Scans directories and subdirectories for files and sends them to QueueManager."""

    def __init__(self, config: SimpleConfig, queue_manager: QueueManager):
        """Initialize file listener.
        
        Args:
            config: Application configuration
            queue_manager: Queue manager to send found files to
        """
        self.config = config
        self.queue_manager = queue_manager

        # File extensions and patterns to ignore
        self.ignore_patterns = {
            # System files
            '.DS_Store', 'Thumbs.db', 'desktop.ini',
            # Temporary files
            '.tmp', '.temp', '.swp', '.swo',
            # Hidden files (starting with .)
            '.*',
            # Log files
            '.log'
        }

        # Directories to ignore
        self.ignore_dirs = {
            '.git', '.svn', '.hg',
            '__pycache__', '.pytest_cache',
            'node_modules', '.venv', 'venv',
            '.aws-copier'  # Our own batch folder
        }

        # Statistics
        self._stats = {
            "scanned_files": 0,
            "ignored_files": 0,
            "queued_files": 0,
            "scanned_directories": 0
        }

    async def scan_all_folders(self) -> None:
        """Scan all configured watch folders and add files to queue."""
        logger.info("Starting initial scan of all watch folders")

        total_files = 0

        for folder_path in self.config.watch_folders:
            if not folder_path.exists():
                logger.warning(f"Watch folder does not exist: {folder_path}")
                continue

            if not folder_path.is_dir():
                logger.warning(f"Watch path is not a directory: {folder_path}")
                continue

            logger.info(f"Scanning folder: {folder_path}")
            files_found = await self._scan_folder(folder_path)
            total_files += files_found

            logger.info(f"Found {files_found} files in {folder_path}")

        logger.info(f"Initial scan completed. Total files found: {total_files}")

    async def _scan_folder(self, folder_path: Path) -> int:
        """Scan a single folder recursively.
        
        Args:
            folder_path: Path to folder to scan
            
        Returns:
            Number of files found and queued
        """
        files_found = 0
        batch_files: List[Path] = []
        batch_size = 1000  # Process files in batches to avoid memory issues

        try:
            # Use rglob to recursively find all files
            for file_path in folder_path.rglob("*"):
                # Skip if it's a directory
                if file_path.is_dir():
                    # Check if we should ignore this directory
                    if self._should_ignore_directory(file_path):
                        continue

                    self._stats["scanned_directories"] += 1
                    continue

                # Skip if it's not a regular file
                if not file_path.is_file():
                    continue

                self._stats["scanned_files"] += 1

                # Check if we should ignore this file
                if self._should_ignore_file(file_path):
                    self._stats["ignored_files"] += 1
                    continue

                # Add to batch
                batch_files.append(file_path)
                files_found += 1

                # Process batch when it reaches the batch size
                if len(batch_files) >= batch_size:
                    await self.queue_manager.add_files(batch_files)
                    self._stats["queued_files"] += len(batch_files)
                    logger.debug(f"Queued batch of {len(batch_files)} files")
                    batch_files.clear()

                # Yield control occasionally to prevent blocking
                if files_found % 100 == 0:
                    await asyncio.sleep(0)

            # Process remaining files in the final batch
            if batch_files:
                await self.queue_manager.add_files(batch_files)
                self._stats["queued_files"] += len(batch_files)
                logger.debug(f"Queued final batch of {len(batch_files)} files")

        except Exception as e:
            logger.error(f"Error scanning folder {folder_path}: {e}")

        return files_found

    def _should_ignore_file(self, file_path: Path) -> bool:
        """Check if a file should be ignored.
        
        Args:
            file_path: Path to file to check
            
        Returns:
            True if file should be ignored, False otherwise
        """
        filename = file_path.name

        # Check if filename starts with dot (hidden file)
        if filename.startswith('.'):
            return True

        # Check ignore patterns
        for pattern in self.ignore_patterns:
            if (pattern.startswith('.') and filename.endswith(pattern)) or pattern == filename:
                return True

        # Check if file is in our batch folder (avoid recursion)
        try:
            file_path.relative_to(self.config.batch_folder)
            return True  # File is in batch folder, ignore it
        except ValueError:
            pass  # File is not in batch folder, continue checking

        return False

    def _should_ignore_directory(self, dir_path: Path) -> bool:
        """Check if a directory should be ignored.
        
        Args:
            dir_path: Path to directory to check
            
        Returns:
            True if directory should be ignored, False otherwise
        """
        dirname = dir_path.name

        # Check if directory name is in ignore list
        if dirname in self.ignore_dirs:
            return True

        # Check if directory starts with dot (hidden)
        if dirname.startswith('.'):
            return True

        # Check if directory is our batch folder (avoid recursion)
        try:
            dir_path.relative_to(self.config.batch_folder)
            return True  # Directory is in batch folder, ignore it
        except ValueError:
            pass  # Directory is not in batch folder, continue checking

        return False

    def get_statistics(self) -> dict:
        """Get scanning statistics.
        
        Returns:
            Dictionary with scanning statistics
        """
        return self._stats.copy()

    def reset_statistics(self) -> None:
        """Reset scanning statistics."""
        self._stats = {
            "scanned_files": 0,
            "ignored_files": 0,
            "queued_files": 0,
            "scanned_directories": 0
        }
