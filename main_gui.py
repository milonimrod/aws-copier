"""AWS Copier main entry point with GUI."""

import asyncio
import logging
import sys
import os
import signal
import threading
import tkinter as tk
from pathlib import Path

from aws_copier.core.file_listener import FileListener
from aws_copier.core.folder_watcher import FolderWatcher
from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig, load_config
from aws_copier.ui.simple_gui import create_gui

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class AWSCopierGUIApp:
    """Main AWS Copier application with GUI support."""

    def __init__(self):
        """Initialize application."""
        self.config = load_config()
        self.s3_manager = S3Manager(self.config)
        # Incremental backup components
        self.file_listener = FileListener(self.config, self.s3_manager)
        self.folder_watcher = FolderWatcher(self.config, self.file_listener)  # Real-time monitoring
        self.running = False
        self.shutdown_event = None

        # GUI components
        self.gui = None
        self.background_thread = None
        self.loop = None

    async def initialize(self):
        """Initialize all components."""
        logger.info("Initializing AWS Copier...")

        # Initialize S3 manager
        await self.s3_manager.initialize()
        logger.info("S3 Manager initialized successfully")

    async def run(self):
        """Run the application with GUI."""
        try:
            await self.initialize()

            # Create GUI on main thread
            self.gui = create_gui(shutdown_callback=self._gui_shutdown_callback)

            # Add initial status message
            logger.info("AWS Copier started with GUI")
            logger.info(f"Watching {len(self.config.watch_folders)} folders")
            for folder in self.config.watch_folders:
                logger.info(f"  - {folder}")

            # Start the main application loop in background
            self._start_background_tasks()

            # Run GUI main loop (blocking)
            self.gui.run()

        except Exception as e:
            logger.error(f"Application error: {e}")
            raise
        finally:
            await self.cleanup()

    def _start_background_tasks(self):
        """Start background tasks for file monitoring."""

        def run_background():
            try:
                # Create new event loop for background thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.loop = loop

                # Run the background tasks
                loop.run_until_complete(self._run_background_loop())
            except Exception as e:
                logger.error(f"Background task error: {e}")
            finally:
                if hasattr(self, "loop") and self.loop:
                    self.loop.close()

        self.background_thread = threading.Thread(target=run_background, daemon=True)
        self.background_thread.start()

    def _gui_shutdown_callback(self):
        """Callback function called when GUI requests shutdown."""
        logger.info("Shutdown requested from GUI")
        if self.loop and self.shutdown_event:
            self.loop.call_soon_threadsafe(self.shutdown_event.set)

    async def _run_background_loop(self):
        """Run the background application loop."""
        self.running = True
        self.shutdown_event = asyncio.Event()

        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

        try:
            # Perform initial scan
            logger.info("Starting initial folder scan...")
            await self.file_listener.scan_all_folders()
            logger.info("Initial scan completed")

            # Start folder watcher for real-time monitoring
            logger.info("Starting folder watcher...")
            await self.folder_watcher.start()
            logger.info("Folder watcher started")

            # Main loop - wait for shutdown signal
            logger.info("AWS Copier is running. Monitoring for file changes...")

            while self.running and not self.shutdown_event.is_set():
                try:
                    # Wait for shutdown event with timeout for responsiveness
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=300)
                    break
                except asyncio.TimeoutError:
                    # Timeout is normal, continue monitoring
                    logger.debug("Heartbeat - AWS Copier still running")
                    continue

        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        except Exception as e:
            logger.error(f"Error in background loop: {e}")
            raise
        finally:
            logger.info("Background tasks shutting down...")
            # Close GUI when background tasks complete
            if self.gui and self.gui.running:
                self.gui.root.after(100, self.gui._close_gui)

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""

        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}")
            if self.shutdown_event:
                asyncio.create_task(self._set_shutdown_event())

        async def _set_shutdown_event():
            """Set shutdown event asynchronously."""
            if self.shutdown_event:
                self.shutdown_event.set()

        self._set_shutdown_event = _set_shutdown_event

        # Register signal handlers based on platform
        if os.name != "nt":  # Unix-like systems (Linux, macOS)
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        else:  # Windows
            signal.signal(signal.SIGINT, signal_handler)
            try:
                signal.signal(signal.SIGBREAK, signal_handler)
            except AttributeError:
                # SIGBREAK not available on all Windows versions
                pass

    async def cleanup(self):
        """Clean up resources."""
        logger.info("Cleaning up resources...")
        self.running = False

        try:
            # Stop folder watcher
            if hasattr(self, "folder_watcher"):
                await self.folder_watcher.stop()
                logger.info("Folder watcher stopped")
        except Exception as e:
            logger.error(f"Error stopping folder watcher: {e}")

        try:
            # Close S3 manager
            if hasattr(self, "s3_manager"):
                await self.s3_manager.close()
                logger.info("S3 Manager closed")
        except Exception as e:
            logger.error(f"Error closing S3 manager: {e}")

        logger.info("AWS Copier shutdown complete")


def main():
    """Main entry point."""
    # Check if GUI is available
    try:
        # Test if tkinter is available
        root = tk.Tk()
        root.withdraw()  # Hide the test window
        root.destroy()
    except Exception as e:
        logger.error(f"GUI not available: {e}")
        logger.error("Please install tkinter or run the console version with: python main.py")
        sys.exit(1)

    # Create and run application
    app = AWSCopierGUIApp()

    try:
        # Run the async initialization and then the GUI
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("Application interrupted")
    except Exception as e:
        logger.error(f"Application failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Example configuration for first run
    config_path = Path("config.yaml")
    if not config_path.exists():
        logger.info("No configuration found. Creating example configuration...")

        example_config = SimpleConfig(
            aws_access_key_id="your-aws-access-key-id",
            aws_secret_access_key="your-aws-secret-access-key",
            aws_region="us-east-1",
            s3_bucket="your-s3-bucket-name",
            s3_prefix="backups/",
            watch_folders=[
                str(Path.home() / "Documents"),
                str(Path.home() / "Pictures"),
            ],
            max_concurrent_uploads=100,
        )

        example_config.save_to_yaml(config_path)
        logger.info(f"Example configuration saved to {config_path}")
        logger.info("Please edit the configuration file with your AWS credentials and settings")
        logger.info("Then run the application again")
        sys.exit(0)

    # Run the application
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
