"""Folder watcher for real-time file changes."""

import asyncio
import logging
from pathlib import Path
from typing import Dict

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from aws_copier.core.queue_manager import QueueManager
from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)


class FileChangeHandler(FileSystemEventHandler):
    """Handles file system change events."""

    def __init__(self, queue_manager: QueueManager, watch_folder: Path):
        """Initialize file change handler.
        
        Args:
            queue_manager: Queue manager to send files to
            watch_folder: Root folder being watched
        """
        super().__init__()
        self.queue_manager = queue_manager
        self.watch_folder = watch_folder

        # File extensions and patterns to ignore (same as FileListener)
        self.ignore_patterns = {
            '.DS_Store', 'Thumbs.db', 'desktop.ini',
            '.tmp', '.temp', '.swp', '.swo',
            '.log'
        }

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Handle any file system event."""
        try:
            # Skip directory events
            if event.is_directory:
                return

            # Only handle file creation and modification
            if event.event_type not in ['created', 'modified']:
                return

            file_path = Path(event.src_path)

            # Skip if file should be ignored
            if self._should_ignore_file(file_path):
                return

            # Skip if file doesn't exist (might have been deleted quickly)
            if not file_path.exists():
                return

            # Add file to queue asynchronously
            asyncio.create_task(self._add_file_to_queue(file_path))

        except Exception as e:
            logger.error(f"Error handling file system event: {e}")

    async def _add_file_to_queue(self, file_path: Path) -> None:
        """Add file to queue asynchronously.
        
        Args:
            file_path: Path to file to add to queue
        """
        try:
            await self.queue_manager.add_file(file_path)
            logger.debug(f"Added file to queue from watcher: {file_path}")
        except Exception as e:
            logger.error(f"Error adding file to queue: {e}")

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

        return False


class FolderWatcher:
    """Watches folders for real-time file changes."""

    def __init__(self, config: SimpleConfig, queue_manager: QueueManager):
        """Initialize folder watcher.
        
        Args:
            config: Application configuration
            queue_manager: Queue manager to send files to
        """
        self.config = config
        self.queue_manager = queue_manager
        self.observer = Observer()
        self.handlers: Dict[str, FileChangeHandler] = {}
        self.running = False

        # Statistics
        self._stats = {
            "watched_folders": 0,
            "events_processed": 0,
            "files_queued": 0
        }

    async def start(self) -> None:
        """Start watching all configured folders."""
        if self.running:
            logger.warning("Folder watcher is already running")
            return

        logger.info("Starting folder watcher")

        # Set up watches for all configured folders
        for folder_path in self.config.watch_folders:
            await self._add_folder_watch(folder_path)

        # Start the observer
        self.observer.start()
        self.running = True

        logger.info(f"Folder watcher started, watching {len(self.handlers)} folders")

    async def stop(self) -> None:
        """Stop watching folders."""
        if not self.running:
            return

        logger.info("Stopping folder watcher")

        # Stop the observer
        self.observer.stop()
        self.observer.join(timeout=5)

        self.running = False
        self.handlers.clear()

        logger.info("Folder watcher stopped")

    async def _add_folder_watch(self, folder_path: Path) -> None:
        """Add a folder to the watch list.
        
        Args:
            folder_path: Path to folder to watch
        """
        if not folder_path.exists():
            logger.error(f"Watch folder does not exist: {folder_path}")
            return

        if not folder_path.is_dir():
            logger.error(f"Watch path is not a directory: {folder_path}")
            return

        # Create event handler
        handler = FileChangeHandler(self.queue_manager, folder_path)

        # Add to observer
        try:
            watch = self.observer.schedule(
                handler,
                str(folder_path),
                recursive=True
            )

            folder_id = str(folder_path)
            self.handlers[folder_id] = handler
            self._stats["watched_folders"] += 1

            logger.info(f"Added watch for folder: {folder_path}")

        except Exception as e:
            logger.error(f"Error adding watch for folder {folder_path}: {e}")

    def get_statistics(self) -> dict:
        """Get watcher statistics.
        
        Returns:
            Dictionary with watcher statistics
        """
        return {
            "running": self.running,
            "observer_threads": len(self.observer.emitters) if self.observer else 0,
            **self._stats
        }

    def is_running(self) -> bool:
        """Check if folder watcher is running.
        
        Returns:
            True if running, False otherwise
        """
        return self.running
