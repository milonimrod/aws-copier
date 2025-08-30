"""
Simple GUI for AWS Copier application.

Provides a minimal interface with:
- Log display area
- Shutdown button
- System tray support (minimize to tray)
"""

import logging
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox
from typing import Optional, Callable
import sys


class LogHandler(logging.Handler):
    """Custom logging handler that sends logs to a queue for GUI display."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        """Send log record to queue."""
        try:
            log_entry = self.format(record)
            self.log_queue.put(log_entry)
        except Exception:
            # Avoid infinite recursion if logging fails
            pass


class AWSCopierGUI:
    """Simple GUI for AWS Copier application."""

    def __init__(self, shutdown_callback: Optional[Callable] = None):
        """Initialize the GUI.

        Args:
            shutdown_callback: Function to call when shutdown is requested
        """
        self.shutdown_callback = shutdown_callback
        self.log_queue = queue.Queue()
        self.running = True

        # Create main window
        self.root = tk.Tk()
        self.root.title("AWS Copier")
        self.root.geometry("800x600")
        self.root.minsize(600, 400)

        # Set window icon (if available)
        self._set_window_icon()

        # Configure window close behavior
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Create GUI elements
        self._create_widgets()

        # Setup logging
        self._setup_logging()

        # Start log processing
        self._start_log_processing()

    def _set_window_icon(self) -> None:
        """Set window icon if available."""
        try:
            # Try to set a simple icon (optional)
            if sys.platform.startswith("win"):
                # On Windows, you could set an .ico file
                pass
            elif sys.platform.startswith("darwin"):
                # On macOS, you could set an .icns file
                pass
            # On Linux, Tkinter will use default
        except Exception:
            # Icon setting is optional
            pass

    def _create_widgets(self) -> None:
        """Create and layout GUI widgets."""
        # Main frame
        main_frame = tk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Title label
        title_label = tk.Label(main_frame, text="AWS Copier - File Backup Monitor", font=("Arial", 14, "bold"))
        title_label.pack(pady=(0, 10))

        # Status frame
        status_frame = tk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(0, 10))

        tk.Label(status_frame, text="Status:", font=("Arial", 10, "bold")).pack(side=tk.LEFT)
        self.status_label = tk.Label(status_frame, text="Running", fg="green", font=("Arial", 10))
        self.status_label.pack(side=tk.LEFT, padx=(5, 0))

        # Log display area
        log_frame = tk.Frame(main_frame)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        tk.Label(log_frame, text="Application Logs:", font=("Arial", 10, "bold")).pack(anchor=tk.W)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            width=80,
            height=20,
            font=("Consolas", 9) if sys.platform.startswith("win") else ("Monaco", 9),
            bg="#f8f9fa",
            fg="#212529",
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(5, 0))

        # Button frame
        button_frame = tk.Frame(main_frame)
        button_frame.pack(fill=tk.X)

        # Clear logs button
        clear_btn = tk.Button(
            button_frame,
            text="Clear Logs",
            command=self._clear_logs,
            bg="#6c757d",
            fg="white",
            font=("Arial", 10),
            padx=20,
        )
        clear_btn.pack(side=tk.LEFT)

        # Minimize button
        minimize_btn = tk.Button(
            button_frame,
            text="Minimize to Tray",
            command=self._minimize_to_tray,
            bg="#17a2b8",
            fg="white",
            font=("Arial", 10),
            padx=20,
        )
        minimize_btn.pack(side=tk.LEFT, padx=(10, 0))

        # Shutdown button
        shutdown_btn = tk.Button(
            button_frame,
            text="Shutdown",
            command=self._shutdown,
            bg="#dc3545",
            fg="white",
            font=("Arial", 10, "bold"),
            padx=20,
        )
        shutdown_btn.pack(side=tk.RIGHT)

    def _setup_logging(self) -> None:
        """Setup logging to capture application logs."""
        # Create custom handler
        self.log_handler = LogHandler(self.log_queue)
        self.log_handler.setLevel(logging.INFO)

        # Create formatter
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S")
        self.log_handler.setFormatter(formatter)

        # Add handler to root logger
        root_logger = logging.getLogger()
        root_logger.addHandler(self.log_handler)

        # Add initial log message
        logging.info("AWS Copier GUI started")

    def _start_log_processing(self) -> None:
        """Start processing logs from the queue."""
        self._process_log_queue()

    def _process_log_queue(self) -> None:
        """Process logs from the queue and display in GUI."""
        try:
            while True:
                try:
                    log_entry = self.log_queue.get_nowait()
                    self._add_log_entry(log_entry)
                except queue.Empty:
                    break
        except Exception as e:
            # Handle any errors in log processing
            self._add_log_entry(f"ERROR: Log processing error: {e}")

        # Schedule next check
        if self.running:
            self.root.after(100, self._process_log_queue)

    def _add_log_entry(self, log_entry: str) -> None:
        """Add a log entry to the display."""
        try:
            # Insert at end
            self.log_text.insert(tk.END, log_entry + "\n")

            # Auto-scroll to bottom
            self.log_text.see(tk.END)

            # Limit log display to last 1000 lines
            lines = self.log_text.get("1.0", tk.END).split("\n")
            if len(lines) > 1000:
                # Remove old lines
                self.log_text.delete("1.0", f"{len(lines) - 1000}.0")

        except Exception:
            # Ignore errors in log display
            pass

    def _clear_logs(self) -> None:
        """Clear the log display."""
        self.log_text.delete("1.0", tk.END)
        logging.info("Log display cleared")

    def _minimize_to_tray(self) -> None:
        """Minimize window (simulate tray behavior)."""
        self.root.iconify()
        logging.info("Application minimized")

    def _on_window_close(self) -> None:
        """Handle window close event."""
        if messagebox.askokcancel("Quit", "Do you want to shutdown AWS Copier?"):
            self._shutdown()
        else:
            # Just minimize instead of closing
            self._minimize_to_tray()

    def _shutdown(self) -> None:
        """Shutdown the application."""
        if messagebox.askokcancel("Shutdown", "Are you sure you want to shutdown AWS Copier?"):
            logging.info("Shutdown requested from GUI")
            self.status_label.config(text="Shutting down...", fg="red")
            self.running = False

            # Call shutdown callback if provided
            if self.shutdown_callback:
                try:
                    # Run shutdown callback in a separate thread to avoid blocking GUI
                    shutdown_thread = threading.Thread(target=self.shutdown_callback, daemon=True)
                    shutdown_thread.start()
                except Exception as e:
                    logging.error(f"Error calling shutdown callback: {e}")

            # Close GUI after a short delay
            self.root.after(1000, self._close_gui)

    def _close_gui(self) -> None:
        """Close the GUI."""
        try:
            # Remove our log handler
            root_logger = logging.getLogger()
            root_logger.removeHandler(self.log_handler)
        except Exception:
            pass

        # Destroy the window
        self.root.quit()
        self.root.destroy()

    def run(self) -> None:
        """Run the GUI main loop."""
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._shutdown()
        except Exception as e:
            logging.error(f"GUI error: {e}")
            self._close_gui()


def create_gui(shutdown_callback: Optional[Callable] = None) -> AWSCopierGUI:
    """Create and return a GUI instance.

    Args:
        shutdown_callback: Function to call when shutdown is requested

    Returns:
        AWSCopierGUI instance
    """
    return AWSCopierGUI(shutdown_callback)


if __name__ == "__main__":
    # Test the GUI
    def test_shutdown():
        print("Shutdown callback called")

    # Create some test logs
    logging.basicConfig(level=logging.INFO)

    gui = create_gui(test_shutdown)

    # Add some test log messages
    logging.info("Test log message 1")
    logging.warning("Test warning message")
    logging.error("Test error message")

    gui.run()
