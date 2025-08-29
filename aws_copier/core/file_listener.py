"""File listener for initial directory scanning."""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List

from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)


class FileListener:
    """Scans directories and subdirectories for files and writes them to discovered files folder."""

    def __init__(self, config: SimpleConfig):
        """Initialize file listener.
        
        Args:
            config: Application configuration
        """
        self.config = config

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
        """Scan all configured watch folders and write found files to discovered files folder."""
        logger.info("Starting initial scan of all watch folders")

        # Ensure discovered files folder exists
        self.config.discovered_files_folder.mkdir(parents=True, exist_ok=True)

        total_files = 0
        all_discovered_files = []

        for folder_path in self.config.watch_folders:
            if not folder_path.exists():
                logger.warning(f"Watch folder does not exist: {folder_path}")
                continue

            if not folder_path.is_dir():
                logger.warning(f"Watch path is not a directory: {folder_path}")
                continue

            logger.info(f"Scanning folder: {folder_path}")
            folder_files = await self._scan_folder(folder_path)
            all_discovered_files.extend(folder_files)
            total_files += len(folder_files)

            logger.info(f"Found {len(folder_files)} files in {folder_path}")

        # Write all discovered files to a single batch file
        if all_discovered_files:
            await self._write_discovered_files(all_discovered_files, "initial_scan")

        logger.info(f"Initial scan completed. Total files found: {total_files}")

    async def _scan_folder(self, folder_path: Path) -> List[str]:
        """Scan a single folder recursively.
        
        Args:
            folder_path: Path to folder to scan
            
        Returns:
            List of file paths found
        """
        discovered_files = []
        
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

                # Add to discovered files list
                discovered_files.append(str(file_path))

                # Yield control occasionally to prevent blocking
                if len(discovered_files) % 100 == 0:
                    await asyncio.sleep(0)

        except Exception as e:
            logger.error(f"Error scanning folder {folder_path}: {e}")

        return discovered_files

    async def _write_discovered_files(self, files: List[str], source: str) -> None:
        """Write discovered files to the discovered files folder.
        
        Args:
            files: List of file paths to write
            source: Source identifier (e.g., 'initial_scan', folder name)
        """
        if not files:
            return

        # Create unique filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{source}_{timestamp}.json"
        output_path = self.config.discovered_files_folder / filename

        # Prepare data to write
        data = {
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "type": "file_list",
            "files": files,
            "count": len(files)
        }

        try:
            with open(output_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.info(f"Wrote {len(files)} files from {source} to {output_path}")
            
        except Exception as e:
            logger.error(f"Failed to write discovered files to {output_path}: {e}")

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
