"""Batch processor that uploads files from batch files to S3."""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional

from aws_copier.core.queue_manager import QueueManager
from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)


class BatchProcessor:
    """Processes batch files and uploads files to S3 with semaphore control."""

    def __init__(self, config: SimpleConfig, s3_manager: S3Manager, queue_manager: QueueManager):
        """Initialize batch processor.
        
        Args:
            config: Application configuration
            s3_manager: S3 manager for uploads
            queue_manager: Queue manager for batch operations
        """
        self.config = config
        self.s3_manager = s3_manager
        self.queue_manager = queue_manager

        # Concurrency control
        self._upload_semaphore = asyncio.Semaphore(config.max_concurrent_uploads)

        # Processing state
        self.running = False
        self._processing_task: Optional[asyncio.Task] = None

        # Statistics
        self._stats = {
            "batches_processed": 0,
            "files_uploaded": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "bytes_uploaded": 0,
            "active_uploads": 0
        }

    async def start(self) -> None:
        """Start the batch processor."""
        if self.running:
            logger.warning("Batch processor is already running")
            return

        logger.info("Starting batch processor")

        # Start processing task
        self._processing_task = asyncio.create_task(self._process_batches())
        self.running = True

        logger.info("Batch processor started")

    async def stop(self) -> None:
        """Stop the batch processor."""
        if not self.running:
            return

        logger.info("Stopping batch processor")

        self.running = False

        # Cancel processing task
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass

        logger.info("Batch processor stopped")

    async def _process_batches(self) -> None:
        """Main processing loop that handles batch files."""
        logger.info("Started batch processing loop")

        try:
            while self.running:
                # Get all batch files
                batch_files = self.queue_manager.get_batch_files()

                if not batch_files:
                    # No batch files to process, wait a bit
                    await asyncio.sleep(1)
                    continue

                # Process oldest batch file first
                batch_files.sort(key=lambda x: x.stat().st_mtime)
                batch_file = batch_files[0]

                await self._process_batch_file(batch_file)

        except asyncio.CancelledError:
            logger.info("Batch processing loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in batch processing loop: {e}")

    async def _process_batch_file(self, batch_file: Path) -> None:
        """Process a single batch file.
        
        Args:
            batch_file: Path to batch file to process
        """
        try:
            logger.info(f"Processing batch file: {batch_file}")

            # Load batch data
            batch_data = self.queue_manager.load_batch_file(batch_file)
            if not batch_data:
                logger.error(f"Failed to load batch file: {batch_file}")
                return

            files = batch_data.get("files", [])
            if not files:
                logger.info(f"Batch file is empty: {batch_file}")
                self.queue_manager.delete_batch_file(batch_file)
                return

            logger.info(f"Processing {len(files)} files from batch")

            # Create upload tasks for all files
            tasks = []
            for file_path_str in files:
                task = asyncio.create_task(
                    self._upload_file_with_semaphore(Path(file_path_str))
                )
                tasks.append(task)

            # Wait for all uploads to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Count results
            successful = sum(1 for r in results if r is True)
            failed = sum(1 for r in results if r is False)
            errors = sum(1 for r in results if isinstance(r, Exception))

            logger.info(f"Batch completed: {successful} successful, {failed} failed, {errors} errors")

            # Update statistics
            self._stats["batches_processed"] += 1
            self._stats["files_uploaded"] += successful
            self._stats["files_failed"] += failed + errors

            # Delete batch file after processing
            if self.queue_manager.delete_batch_file(batch_file):
                logger.info(f"Deleted processed batch file: {batch_file}")
            else:
                logger.error(f"Failed to delete batch file: {batch_file}")

        except Exception as e:
            logger.error(f"Error processing batch file {batch_file}: {e}")

    async def _upload_file_with_semaphore(self, file_path: Path) -> bool:
        """Upload a file with semaphore control.
        
        Args:
            file_path: Path to file to upload
            
        Returns:
            True if upload successful, False otherwise
        """
        async with self._upload_semaphore:
            self._stats["active_uploads"] += 1
            try:
                return await self._upload_file(file_path)
            finally:
                self._stats["active_uploads"] -= 1

    async def _upload_file(self, file_path: Path) -> bool:
        """Upload a single file to S3.
        
        Args:
            file_path: Path to file to upload
            
        Returns:
            True if upload successful, False otherwise
        """
        try:
            # Check if file still exists
            if not file_path.exists():
                logger.debug(f"File no longer exists, skipping: {file_path}")
                self._stats["files_skipped"] += 1
                return True  # Not an error, just skip

            # Generate S3 key from file path
            s3_key = self._generate_s3_key(file_path)

            # Check if file already exists in S3 with same content
            file_size = file_path.stat().st_size

            # Calculate MD5 for existence check
            md5_hash = await self.s3_manager._calculate_md5(file_path)
            if not md5_hash:
                logger.error(f"Failed to calculate MD5 for: {file_path}")
                return False

            # Check if file already exists with same MD5
            if await self.s3_manager.check_exists(s3_key, md5_hash):
                logger.debug(f"File already exists in S3 with same content, skipping: {file_path}")
                self._stats["files_skipped"] += 1
                return True

            # Upload file
            success = await self.s3_manager.upload_file(file_path, s3_key)

            if success:
                self._stats["bytes_uploaded"] += file_size
                logger.debug(f"Upload successful: {file_path}")
                return True
            logger.error(f"Upload failed: {file_path}")
            return False

        except Exception as e:
            logger.error(f"Error uploading file {file_path}: {e}")
            return False

    def _generate_s3_key(self, file_path: Path) -> str:
        """Generate S3 key from file path.
        
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
                folder_name = watch_folder.name
                s3_key = f"{folder_name}/{relative_path}"

                return s3_key.replace("\\", "/")  # Ensure forward slashes for S3

            except ValueError:
                # File is not under this watch folder
                continue

        # Fallback: use full path if not under any watch folder
        return str(file_path).replace("\\", "/")

    def get_statistics(self) -> Dict[str, any]:
        """Get batch processor statistics.
        
        Returns:
            Statistics dictionary
        """
        return {
            "running": self.running,
            "semaphore_available": self._upload_semaphore._value,
            "max_concurrent_uploads": self.config.max_concurrent_uploads,
            **self._stats
        }

    def reset_statistics(self) -> None:
        """Reset statistics counters."""
        self._stats = {
            "batches_processed": 0,
            "files_uploaded": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "bytes_uploaded": 0,
            "active_uploads": self._stats["active_uploads"]  # Keep active count
        }
