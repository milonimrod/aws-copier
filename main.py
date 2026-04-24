"""AWS Copier main entry point."""

import asyncio
import logging
import signal
import sys
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
            # Initialize S3 manager
            await self.s3_manager.initialize()
            logger.info("✅ S3 Manager initialized")

            # ASYNC-06: install SIGTERM / SIGINT handlers now that the loop is running.
            self._setup_signal_handlers()

            # Run incremental backup scan of all folders
            await self.file_listener.scan_all_folders()

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

    async def shutdown(self) -> None:
        """Shutdown the application (ASYNC-06): stop the watcher, drain in-flight uploads, close S3."""
        if not self.running:
            # shutdown may be called from the signal handler AND from the main-loop finally;
            # short-circuit the second call.
            return

        logger.info("Shutting down AWS Copier...")
        self.running = False

        try:
            # Step 1: stop the folder watcher so no NEW events create new upload tasks.
            await self.folder_watcher.stop()
            logger.info("✅ Folder Watcher stopped")

            # Step 2 (ASYNC-06): drain in-flight uploads for up to 60s (D-03).
            # Pitfall 3 guard: asyncio.wait raises ValueError on an empty set, so check first.
            upload_tasks = set(self.file_listener._active_upload_tasks)
            if upload_tasks:
                logger.info(f"Draining {len(upload_tasks)} in-flight upload(s) (max 60s)")
                done, pending = await asyncio.wait(upload_tasks, timeout=60)
                if pending:
                    # D-04: name each abandoned file so the user knows what the next scan cycle will re-check.
                    for task in pending:
                        logger.warning(f"Abandoned in-flight upload: {task.get_name()}")
                        task.cancel()
                logger.info(f"Drain complete: {len(done)} finished, {len(pending)} abandoned")
            else:
                logger.info("No in-flight uploads to drain")

            # Step 3: close S3 client.
            await self.s3_manager.close()
            logger.info("✅ S3 Manager closed")

            logger.info("🛑 AWS Copier stopped successfully")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)

    def _setup_signal_handlers(self) -> None:
        """Register SIGTERM/SIGINT handlers on the running asyncio loop (ASYNC-06).

        Unix (sys.platform != 'win32'): uses loop.add_signal_handler, which delivers
        signals directly to the running event loop.

        Windows (sys.platform == 'win32'): loop.add_signal_handler is not supported;
        falls back to signal.signal with a synchronous handler that schedules the
        async shutdown path via loop.call_soon_threadsafe(asyncio.ensure_future, ...).
        """
        loop = asyncio.get_running_loop()

        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.ensure_future(self._handle_signal(s)),
                )
            logger.info("Signal handlers registered (Unix): SIGTERM, SIGINT")
        else:
            # Windows fallback: loop.add_signal_handler is not implemented on ProactorEventLoop.
            def _win_handler(signum: int, _frame: object) -> None:
                loop.call_soon_threadsafe(asyncio.ensure_future, self._handle_signal(signum))

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    signal.signal(sig, _win_handler)
                except (ValueError, OSError):
                    # SIGTERM is not fully supported on some Windows builds; log and continue.
                    logger.warning(f"Could not install signal handler for {sig} on Windows")
            logger.info("Signal handlers registered (Windows): SIGINT, SIGTERM (via signal.signal)")

    async def _handle_signal(self, signum: int) -> None:
        """Signal-triggered async handler: flip running=False and set the shutdown event.

        Args:
            signum: The signal number received
        """
        logger.info(f"Received signal {signum}; initiating graceful shutdown (drain max 60s)")
        self.running = False
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
