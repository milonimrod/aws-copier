"""AWS Copier main entry point."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

from aws_copier.core.batch_processor import BatchProcessor
from aws_copier.core.file_listener import FileListener
from aws_copier.core.folder_watcher import FolderWatcher
from aws_copier.core.queue_manager import QueueManager
from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig, load_config

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class AWSCopierApp:
    """Main AWS Copier application using simplified architecture."""

    def __init__(self):
        """Initialize application."""
        self.config = load_config()
        self.s3_manager = S3Manager(self.config)
        self.queue_manager = QueueManager(self.config)
        self.file_listener = FileListener(self.config, self.queue_manager)
        self.folder_watcher = FolderWatcher(self.config, self.queue_manager)
        self.batch_processor = BatchProcessor(
            self.config, self.s3_manager, self.queue_manager
        )
        self.running = False

    async def start(self):
        """Start the application."""
        logger.info("Starting AWS Copier (Simplified Architecture)...")

        try:
            # Setup signal handlers
            self._setup_signal_handlers()

            # Initialize S3 manager
            await self.s3_manager.initialize()
            logger.info("✅ S3 Manager initialized")

            # Start queue manager
            await self.queue_manager.start()
            logger.info("✅ Queue Manager started")

            # Start batch processor
            await self.batch_processor.start()
            logger.info("✅ Batch Processor started")

            # Run initial scan of all folders
            await self.file_listener.scan_all_folders()
            logger.info("✅ File Listener completed initial scan")

            # Start folder watcher for real-time monitoring
            await self.folder_watcher.start()
            logger.info("✅ Folder Watcher started (real-time monitoring)")

            self.running = True
            logger.info("🚀 AWS Copier started successfully")

            # Main status loop
            while self.running:
                queue_stats = self.queue_manager.get_statistics()
                processor_stats = self.batch_processor.get_statistics()

                logger.info(
                    f"📊 Status: Queue={queue_stats['total_queued_files']} files, "
                    f"Processing={processor_stats['active_uploads']}/{self.config.max_concurrent_uploads} uploads, "
                    f"Uploaded={processor_stats['files_uploaded']}, "
                    f"Failed={processor_stats['files_failed']}"
                )
                await asyncio.sleep(30)  # Status update every 30 seconds

        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Shutdown the application."""
        if not self.running:
            return

        logger.info("Shutting down AWS Copier...")
        self.running = False

        try:
            # Stop components in reverse order
            await self.folder_watcher.stop()
            logger.info("✅ Folder Watcher stopped")

            # File listener doesn't need to be stopped (no background tasks)
            logger.info("✅ File Listener complete")

            await self.batch_processor.stop()
            logger.info("✅ Batch Processor stopped")

            await self.queue_manager.stop()
            logger.info("✅ Queue Manager stopped")

            await self.s3_manager.close()
            logger.info("✅ S3 Manager closed")

            logger.info("🛑 AWS Copier stopped successfully")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""

        def signal_handler(signum, _):
            logger.info(f"Received signal {signum}")
            self.running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)


async def main():
    """Main entry point."""
    # Check if configuration exists
    config_path = Path("config.yaml")

    if not config_path.exists():
        logger.error(f"Configuration file not found at {config_path}")
        logger.info("Please create a configuration file first.")
        logger.info("Creating example configuration...")

        # Create example config
        example_config = SimpleConfig(
            aws_access_key_id="your-access-key-id",
            aws_secret_access_key="your-secret-access-key",
            s3_bucket="your-bucket-name",
            s3_prefix="backup",
            watch_folders=[str(Path.home() / "Documents")],
            batch_folder="./batches",
        )

        # Save example config
        example_config.save_to_yaml(config_path)

        logger.info(f"Example configuration created at {config_path}")
        logger.info(
            "Please edit the configuration file with your AWS credentials and restart."
        )
        return 1

    # Start application
    app = AWSCopierApp()
    await app.start()
    return 0


def sync_main():
    """Synchronous main entry point for setuptools."""
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Application interrupted")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    sync_main()
