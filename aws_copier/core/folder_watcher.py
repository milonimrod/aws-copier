"""Folder watcher for real-time file changes."""

import asyncio
import logging
from pathlib import Path
from typing import Dict

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)


class FileChangeHandler(FileSystemEventHandler):
    """Handles file system change events."""

    def __init__(self, config: SimpleConfig, watch_folder: Path):
        """Initialize file change handler.
        
        Args:
            config: Application configuration 
            watch_folder: Root folder being watched
        """
        super().__init__()
        self.config = config
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

            # Write file to discovered files folder asynchronously
            asyncio.create_task(self._write_discovered_file(file_path, event.event_type))

        except Exception as e:
            logger.error(f"Error handling file system event: {e}")

    async def _write_discovered_file(self, file_path: Path, event_type: str) -> None:
        """Write a single discovered file to the discovered files folder.
        
        Args:
            file_path: Path to file that was discovered
            event_type: Type of file system event (created, modified)
        """
        try:
            # Ensure discovered files folder exists
            self.config.discovered_files_folder.mkdir(parents=True, exist_ok=True)
            
            # Create unique filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"watcher_{event_type}_{timestamp}.json"
            output_path = self.config.discovered_files_folder / filename

            # Prepare data to write
            data = {
                "timestamp": datetime.now().isoformat(),
                "source": f"watcher_{self.watch_folder.name}",
                "type": "single_file",
                "event_type": event_type,
                "file": str(file_path),
                "watch_folder": str(self.watch_folder)
            }

            with open(output_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.debug(f"Wrote {event_type} file from watcher to {output_path}: {file_path}")
            
        except Exception as e:
            logger.error(f"Error writing discovered file {file_path}: {e}")

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
    """Watches folders for real-time file changes and writes them to discovered files folder."""

    def __init__(self, config: SimpleConfig):
        """Initialize folder watcher.
        
        Args:
            config: Application configuration
        """
        self.config = config
        self.observer = Observer()
        self.handlers: Dict[str, FileChangeHandler] = {}
        self.running = False

        # Statistics
        self._stats = {
            "watched_folders": 0,
            "events_processed": 0,
            "files_written": 0
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
        handler = FileChangeHandler(self.config, folder_path)

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
