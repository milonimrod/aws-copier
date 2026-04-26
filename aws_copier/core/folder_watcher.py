"""Folder watcher for real-time file changes."""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from aws_copier.core.file_listener import FileListener
from aws_copier.core.ignore_rules import IGNORE_RULES
from aws_copier.models.simple_config import SimpleConfig


logger = logging.getLogger(__name__)


class FileChangeHandler(FileSystemEventHandler):
    """Handles file system change events."""

    def __init__(
        self,
        config: SimpleConfig,
        watch_folder: Path,
        file_listener: FileListener,
        event_loop: asyncio.AbstractEventLoop,
    ):
        """Initialize file change handler.

        Args:
            config: Application configuration
            watch_folder: Root folder being watched
            file_listener: FileListener instance for processing changed files
            event_loop: Event loop to schedule async tasks
        """
        super().__init__()
        self.config = config
        self.watch_folder = watch_folder
        self.file_listener = file_listener
        self.event_loop = event_loop
        # PERF-04: per-path debounce state. Dict[str, asyncio.Task] is accessed only
        # from the asyncio event loop thread (via run_coroutine_threadsafe), so no
        # additional locking is needed. The 2-second window and per-path keying are
        # locked by D-06.
        self._debounce_tasks: Dict[str, asyncio.Task] = {}

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Handle any file system event."""
        try:
            # Skip directory events
            if event.is_directory:
                return

            # Only handle file creation and modification
            if event.event_type not in ["created", "modified"]:
                return

            file_path = Path(event.src_path)

            # Skip if file should be ignored (IGNORE-03: delegate to canonical IGNORE_RULES singleton)
            if IGNORE_RULES.should_ignore_file(file_path):
                return

            # Skip if file doesn't exist (might have been deleted quickly)
            if not file_path.exists():
                return

            # Schedule async processing via run_coroutine_threadsafe (ASYNC-01).
            # PERF-04: route through _schedule_debounced so a 2-second per-path window
            # collapses rapid events (atomic-save patterns) into one _process_changed_file call.
            # Returns a concurrent.futures.Future; do NOT call .result() here — that would
            # deadlock the watchdog thread waiting on the main asyncio loop.
            asyncio.run_coroutine_threadsafe(
                self._schedule_debounced(file_path, event.event_type),
                self.event_loop,
            )

        except Exception as e:
            logger.error(f"Error handling file system event: {e}")

    async def _schedule_debounced(self, file_path: Path, event_type: str) -> None:
        """Schedule a debounced _process_changed_file call for file_path.

        D-06: Cancels any pending debounce task for this path and creates a fresh
        2-second timer. Dict access is safe — this coroutine always runs in the
        asyncio event loop thread (scheduled via run_coroutine_threadsafe).

        Args:
            file_path: Path that triggered the file system event
            event_type: watchdog event type string (created, modified, etc.)
        """
        key = str(file_path)
        existing = self._debounce_tasks.get(key)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(
            self._debounced_process(file_path, event_type),
            name=f"debounce-{file_path.name}",
        )
        self._debounce_tasks[key] = task

    async def _debounced_process(self, file_path: Path, event_type: str) -> None:
        """Sleep 2 seconds then delegate to _process_changed_file.

        CancelledError is caught and silenced — it means a newer event superseded
        this one, which is normal behaviour, not an error.

        Args:
            file_path: Path to process after debounce window elapses
            event_type: watchdog event type string
        """
        try:
            await asyncio.sleep(2)
            await self._process_changed_file(file_path, event_type)
        except asyncio.CancelledError:
            # Superseded by a newer event for the same path — normal, not an error.
            pass

    def cancel_all_pending(self) -> None:
        """Cancel all pending debounce tasks (Pitfall 2 guard).

        Called from FolderWatcher.stop() before observer.stop() to prevent
        "RuntimeError: Event loop is closed" from tasks firing after shutdown.

        Returns:
            None
        """
        for task in list(self._debounce_tasks.values()):
            if not task.done():
                task.cancel()
        self._debounce_tasks.clear()

    async def _process_changed_file(self, file_path: Path, event_type: str) -> None:
        """Process a changed file using incremental backup logic.

        Args:
            file_path: Path to file that was changed
            event_type: Type of file system event (created, modified)
        """
        try:
            # Get the folder containing this file
            folder_path = file_path.parent

            # Only process files that are within our watch folders
            is_in_watch_folder = False
            for watch_folder in self.config.watch_folders:
                try:
                    file_path.relative_to(watch_folder)
                    is_in_watch_folder = True
                    break
                except ValueError:
                    continue

            if not is_in_watch_folder:
                return

            logger.info(f"📁 File {event_type}: {file_path}")

            # Process just this folder using incremental backup
            await self.file_listener._process_current_folder(folder_path)

            logger.debug(f"✅ Processed {event_type} file: {file_path}")

        except Exception as e:
            logger.error(f"Error processing changed file {file_path}: {e}")


class FolderWatcher:
    """Watches folders for real-time file changes and processes them with incremental backup."""

    def __init__(self, config: SimpleConfig, file_listener: FileListener):
        """Initialize folder watcher.

        Args:
            config: Application configuration
            file_listener: FileListener instance for processing changed files
        """
        self.config = config
        self.file_listener = file_listener
        self.observer = Observer()
        self.handlers: Dict[str, FileChangeHandler] = {}
        self.running = False
        self.event_loop: Optional[asyncio.AbstractEventLoop] = None

        # Statistics
        self._stats = {"watched_folders": 0, "events_processed": 0, "files_processed": 0}

    async def start(self) -> None:
        """Start watching all configured folders."""
        if self.running:
            logger.warning("Folder watcher is already running")
            return

        # Get the current event loop
        self.event_loop = asyncio.get_running_loop()

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

        # PERF-04 / Pitfall 2: cancel pending debounce tasks before stopping the
        # observer so they cannot fire after the event loop starts shutting down.
        for handler in self.handlers.values():
            handler.cancel_all_pending()

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
        if not self.event_loop:
            raise RuntimeError("Event loop not available. Call start() first.")
        handler = FileChangeHandler(self.config, folder_path, self.file_listener, self.event_loop)

        # Add to observer
        try:
            self.observer.schedule(handler, str(folder_path), recursive=True)

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
            **self._stats,
        }

    def is_running(self) -> bool:
        """Check if folder watcher is running.

        Returns:
            True if running, False otherwise
        """
        return self.running
