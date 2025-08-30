"""Test script for AWS Copier GUI."""

import logging
import time
import threading
from aws_copier.ui.simple_gui import create_gui

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def simulate_aws_copier_logs():
    """Simulate AWS Copier application logs for testing."""

    def log_worker():
        messages = [
            "AWS Copier initialized successfully",
            "S3 Manager connected to bucket: my-backup-bucket",
            "Starting initial folder scan...",
            "Scanning folder: /Users/user/Documents",
            "Found 15 files to upload",
            "Uploading file: document1.pdf",
            "Uploading file: spreadsheet.xlsx",
            "Upload completed: document1.pdf (2.3 MB)",
            "Upload completed: spreadsheet.xlsx (1.1 MB)",
            "Folder scan completed - 15 files uploaded",
            "Starting real-time monitoring...",
            "File watcher started for 2 folders",
            "AWS Copier is now monitoring for changes",
        ]

        for i, message in enumerate(messages):
            time.sleep(2)  # Simulate time between operations

            if i < 3:
                logger.info(message)
            elif i < 8:
                logger.info(message)
            elif i < 10:
                logger.warning(f"Processing: {message}")
            else:
                logger.info(message)

        # Simulate ongoing monitoring
        file_count = 1
        while True:
            time.sleep(10)  # Every 10 seconds
            logger.info(f"Heartbeat - Monitoring active, {file_count} files processed")
            file_count += 1

            # Simulate occasional file changes
            if file_count % 3 == 0:
                logger.info(f"File change detected: new_file_{file_count}.txt")
                time.sleep(1)
                logger.info(f"Upload completed: new_file_{file_count}.txt")

    # Start log simulation in background thread
    log_thread = threading.Thread(target=log_worker, daemon=True)
    log_thread.start()


def test_shutdown():
    """Test shutdown callback."""
    logger.info("Shutdown callback triggered - stopping AWS Copier...")
    logger.info("All uploads completed")
    logger.info("S3 connections closed")
    logger.info("File monitoring stopped")
    logger.info("AWS Copier shutdown complete")


def main():
    """Test the GUI with simulated logs."""
    logger.info("Starting AWS Copier GUI test...")

    # Start simulating logs
    simulate_aws_copier_logs()

    # Create and run GUI
    gui = create_gui(shutdown_callback=test_shutdown)

    logger.info("GUI initialized - you should see the window now")
    logger.info("The GUI will show simulated AWS Copier logs")
    logger.info("Click 'Shutdown' to test the shutdown process")

    # Run the GUI
    gui.run()

    logger.info("GUI closed")


if __name__ == "__main__":
    main()
