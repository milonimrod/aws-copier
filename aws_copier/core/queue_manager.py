"""Queue manager for batch file operations."""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)


class QueueManager:
    """Manages file batches and saves them to disk every N seconds."""

    def __init__(self, config: SimpleConfig):
        """Initialize queue manager."""
        self.config = config
        self.batch_folder = config.batch_folder
        self.save_interval = config.batch_save_interval

        # Current batch of files to process
        self._current_batch: Set[str] = set()
        self._batch_lock = asyncio.Lock()

        # Task for periodic saving
        self._save_task: Optional[asyncio.Task] = None
        self._running = False

        # Statistics
        self._stats = {
            "files_added": 0,
            "batches_saved": 0,
            "last_save_time": None
        }

    async def start(self) -> None:
        """Start the queue manager."""
        if self._running:
            return

        logger.info("Starting queue manager")

        # Create batch directory
        self.batch_folder.mkdir(parents=True, exist_ok=True)

        # Start periodic save task
        self._save_task = asyncio.create_task(self._periodic_save())
        self._running = True

        logger.info(f"Queue manager started, batch folder: {self.batch_folder}")

    async def stop(self) -> None:
        """Stop the queue manager."""
        if not self._running:
            return

        logger.info("Stopping queue manager")

        self._running = False

        # Cancel save task
        if self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass

        # Save any remaining files
        await self._save_current_batch()

        logger.info("Queue manager stopped")

    async def add_file(self, file_path: Path) -> None:
        """Add a file to the current batch.
        
        Args:
            file_path: Path to file to add to batch
        """
        try:
            # Convert to absolute path string for consistency
            file_str = str(file_path.resolve())

            async with self._batch_lock:
                if file_str not in self._current_batch:
                    self._current_batch.add(file_str)
                    self._stats["files_added"] += 1
                    logger.debug(f"Added file to batch: {file_path}")

        except Exception as e:
            logger.error(f"Error adding file to batch: {e}")

    async def add_files(self, file_paths: List[Path]) -> None:
        """Add multiple files to the current batch.
        
        Args:
            file_paths: List of file paths to add
        """
        try:
            file_strings = [str(p.resolve()) for p in file_paths]

            async with self._batch_lock:
                for file_str in file_strings:
                    if file_str not in self._current_batch:
                        self._current_batch.add(file_str)
                        self._stats["files_added"] += 1

                logger.debug(f"Added {len(file_strings)} files to batch")

        except Exception as e:
            logger.error(f"Error adding files to batch: {e}")

    async def _periodic_save(self) -> None:
        """Periodically save the current batch to disk."""
        logger.info(f"Started periodic save task (interval: {self.save_interval}s)")

        try:
            while self._running:
                await asyncio.sleep(self.save_interval)

                if self._running:  # Check again after sleep
                    await self._save_current_batch()

        except asyncio.CancelledError:
            logger.info("Periodic save task cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in periodic save task: {e}")

    async def _save_current_batch(self) -> None:
        """Save current batch to a file if it contains files."""
        async with self._batch_lock:
            if not self._current_batch:
                return  # Nothing to save

            # Create batch data
            batch_data = {
                "created_at": datetime.utcnow().isoformat(),
                "file_count": len(self._current_batch),
                "files": list(self._current_batch)
            }

            # Generate unique batch filename
            batch_id = str(uuid4())[:8]
            timestamp = int(time.time())
            batch_filename = f"batch_{timestamp}_{batch_id}.json"
            batch_path = self.batch_folder / batch_filename

            try:
                # Save batch to file
                with open(batch_path, 'w') as f:
                    json.dump(batch_data, f, indent=2)

                logger.info(f"Saved batch with {len(self._current_batch)} files: {batch_filename}")

                # Update statistics
                self._stats["batches_saved"] += 1
                self._stats["last_save_time"] = datetime.utcnow().isoformat()

                # Clear current batch
                self._current_batch.clear()

            except Exception as e:
                logger.error(f"Error saving batch to {batch_path}: {e}")

    def get_batch_files(self) -> List[Path]:
        """Get list of all batch files in the batch folder.
        
        Returns:
            List of batch file paths
        """
        try:
            return list(self.batch_folder.glob("batch_*.json"))
        except Exception as e:
            logger.error(f"Error listing batch files: {e}")
            return []

    def load_batch_file(self, batch_path: Path) -> Optional[Dict[str, Any]]:
        """Load a batch file and return its contents.
        
        Args:
            batch_path: Path to batch file
            
        Returns:
            Batch data dict or None if error
        """
        try:
            with open(batch_path) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading batch file {batch_path}: {e}")
            return None

    def delete_batch_file(self, batch_path: Path) -> bool:
        """Delete a batch file.
        
        Args:
            batch_path: Path to batch file to delete
            
        Returns:
            True if deleted successfully, False otherwise
        """
        try:
            batch_path.unlink()
            logger.info(f"Deleted batch file: {batch_path}")
            return True
        except Exception as e:
            logger.error(f"Error deleting batch file {batch_path}: {e}")
            return False

    def get_total_queued_files(self) -> int:
        """Get total number of files in all batch files plus current batch.
        
        Returns:
            Total number of queued files
        """
        total = 0

        # Count files in current batch
        total += len(self._current_batch)

        # Count files in saved batch files
        for batch_path in self.get_batch_files():
            batch_data = self.load_batch_file(batch_path)
            if batch_data:
                total += batch_data.get("file_count", 0)

        return total

    def get_batch_file_count(self) -> int:
        """Get number of batch files waiting to be processed.
        
        Returns:
            Number of batch files
        """
        return len(self.get_batch_files())

    def get_statistics(self) -> Dict[str, Any]:
        """Get queue manager statistics.
        
        Returns:
            Statistics dictionary
        """
        return {
            "current_batch_size": len(self._current_batch),
            "total_queued_files": self.get_total_queued_files(),
            "batch_files_count": self.get_batch_file_count(),
            "running": self._running,
            **self._stats
        }

    async def force_save(self) -> None:
        """Force save the current batch immediately."""
        await self._save_current_batch()
        logger.info("Forced save of current batch completed")
