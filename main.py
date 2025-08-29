"""AWS Copier main entry point."""

import asyncio
import logging
import sys
import os
import signal
from pathlib import Path

from aws_copier.core.file_listener import FileListener
from aws_copier.core.folder_watcher import FolderWatcher
from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig, load_config

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class AWSCopierApp:
    """Main AWS Copier application using simplified architecture."""

    def __init__(self):
        """Initialize application."""
        self.config = load_config()
        self.s3_manager = S3Manager(self.config)
        # Incremental backup components
        self.file_listener = FileListener(self.config, self.s3_manager)
        self.folder_watcher = FolderWatcher(self.config, self.file_listener)  # Real-time monitoring
        self.running = False
        self.shutdown_event = asyncio.Event()

    async def start(self):
        """Start the application."""
        logger.info("Starting AWS Copier (Simplified Architecture)...")

        try:
            # Setup signal handlers

            # Initialize S3 manager
            await self.s3_manager.initialize()
            logger.info("✅ S3 Manager initialized")

            # Run incremental backup scan of all folders
            await self.file_listener.scan_all_folders()

            # self._setup_signal_handlers()

            stats = self.file_listener.get_statistics()
            logger.info(f"✅ Incremental backup completed: {stats}")

            # Start folder watcher for real-time monitoring
            await self.folder_watcher.start()
            logger.info("✅ Folder Watcher started")

            self.running = True
            logger.info("🚀 AWS Copier started successfully")

            # Main status loop - show backup statistics
            while self.running:
                stats = self.file_listener.get_statistics()
                logger.info(f"📊 Backup Status: {stats}")

                # Wait for shutdown event or timeout (5 minutes)
                try:
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=300)
                    break  # Shutdown event was set
                except asyncio.TimeoutError:
                    # Timeout reached, continue loop for next status update
                    continue

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
            # Stop components
            await self.folder_watcher.stop()
            logger.info("✅ Folder Watcher stopped")

            # File listener doesn't need to be stopped (no background tasks)
            logger.info("✅ File Listener complete")

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
            # Set the shutdown event to immediately wake up the main loop
            if hasattr(self, "shutdown_event"):
                asyncio.create_task(self._set_shutdown_event())

        # Setup signal handlers based on platform
        if os.name == "nt":  # Windows
            # Windows only supports SIGINT and SIGTERM
            signal.signal(signal.SIGINT, signal_handler)
            # SIGTERM is not supported on Windows, but SIGBREAK is similar
            try:
                signal.signal(signal.SIGBREAK, signal_handler)
            except AttributeError:
                # SIGBREAK might not be available on all Windows versions
                pass
        else:  # Unix-like (macOS, Linux)
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

    async def _set_shutdown_event(self):
        """Set the shutdown event asynchronously."""
        self.shutdown_event.set()


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
        )

        # Save example config
        example_config.save_to_yaml(config_path)

        logger.info(f"Example configuration created at {config_path}")
        logger.info("Please edit the configuration file with your AWS credentials and restart.")
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
