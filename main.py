"""AWS Copier main entry point."""

import asyncio
import ctypes
import gc
import logging
import signal
import sys
import time
from pathlib import Path

from aws_copier.core.file_listener import FileListener
from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig, load_config
from aws_copier.web.dashboard import WebDashboard

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# RELIAB-01: this app has no real-time file watcher — periodic full scans are the only sync
# mechanism, by design. An earlier version paired a real-time watchdog-based watcher with a
# 12h periodic scan as a safety net; the watcher was removed entirely (both real bugs found
# in production were in that subsystem specifically — debounce keyed per-file instead of
# per-folder causing duplicate uploads, and ignored-ancestor-directory files slipping through
# — and a NAS-bundled cloud-sync tool's continuous filesystem churn made inotify queue
# overflow, and therefore silently missed events, a real risk on top of that). For a personal
# backup tool, bounded periodic-scan latency is an acceptable tradeoff for removing an entire
# class of bugs and the watchdog dependency outright, rather than patching around them.
FULL_RESCAN_INTERVAL_SECONDS = 6 * 60 * 60


def _trim_memory() -> None:
    """Best-effort: ask glibc to release freed heap arenas back to the OS (MEM-01, Linux only).

    gc.collect() alone often doesn't shrink RSS on Linux — glibc's malloc keeps freed arenas
    around for reuse rather than returning them to the OS, which is what makes a long-running
    daemon's memory usage look like it never comes back down after a large initial scan even
    once the objects are gone. malloc_trim(0) asks it to release what it safely can. Silently
    does nothing on platforms without glibc (macOS, Windows) or if the call is unavailable.
    """
    if sys.platform != "linux":
        return
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception as exc:
        logger.debug(f"malloc_trim unavailable: {exc}")


def _disable_quickedit_windows() -> None:
    """Disable PowerShell/cmd QuickEdit mode on Windows so the process never freezes on click.

    QuickEdit mode intercepts mouse clicks to start a selection, which pauses the process
    until the user presses Enter/Escape. Disabling it lets the daemon run unattended.
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # STD_INPUT_HANDLE = -10
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_ulong(0)
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # Clear ENABLE_QUICK_EDIT_MODE (0x0040) and ENABLE_MOUSE_INPUT (0x0010),
            # keep ENABLE_EXTENDED_FLAGS (0x0080) so the clear takes effect.
            new_mode = (mode.value & ~0x0040 & ~0x0010) | 0x0080
            kernel32.SetConsoleMode(handle, new_mode)
    except Exception as exc:
        logger.warning(f"Could not disable QuickEdit mode: {exc}")


class AWSCopierApp:
    """Main AWS Copier application using simplified architecture."""

    def __init__(self, config_path: Path = Path("config.yaml")):
        """Initialize application.

        Args:
            config_path: Path to the YAML config file. `main()` already verifies this
                path exists before constructing AWSCopierApp, so load_config() here
                loads it directly rather than falling back to DEFAULT_CONFIG_PATH.
        """
        self.config = load_config(config_path)
        self.s3_manager = S3Manager(self.config)
        # Incremental backup components
        self.file_listener = FileListener(self.config, self.s3_manager)
        self.web_dashboard = (
            WebDashboard(self.file_listener, port=self.config.web_port) if self.config.web_enabled else None
        )
        self.running = False
        self._shutdown_called = False  # re-entrancy guard, separate from self.running
        self.shutdown_event = asyncio.Event()
        # RELIAB-01: set to time.monotonic() right after each full scan completes (initial
        # or periodic); compared against in the status loop to trigger the next one.
        self._last_full_scan_monotonic: float = 0.0

    async def start(self):
        """Start the application."""
        logger.info("Starting AWS Copier (Simplified Architecture)...")

        try:
            # Register signal handlers first so Ctrl+C / SIGTERM work during init.
            self._setup_signal_handlers()

            # Start web dashboard early so log capture begins immediately.
            if self.web_dashboard is not None:
                await self.web_dashboard.start()

            # Initialize S3 manager — 30 s timeout guards against credential/network hangs.
            await asyncio.wait_for(self.s3_manager.initialize(), timeout=30)
            logger.info("✅ S3 Manager initialized")

            # CONFIG-07: ensure AbortIncompleteMultipartUpload lifecycle rule exists.
            # D-11: never raises; logs warning on failure and continues.
            await asyncio.wait_for(self.s3_manager.ensure_lifecycle_rule(), timeout=30)

            # D-10: log credential source for audit trail (set by SimpleConfig per CONFIG-05).
            logger.info(f"AWS credentials loaded from: {self.config.credential_source}")

            # Run incremental backup scan of all folders
            await self.file_listener.scan_all_folders()
            self._last_full_scan_monotonic = time.monotonic()

            stats = self.file_listener.get_statistics()
            logger.info(f"✅ Incremental backup completed: {stats}")

            self.running = True
            logger.info("🚀 AWS Copier started successfully")

            # Main status loop - show backup statistics
            while self.running:
                stats = self.file_listener.get_statistics()
                logger.info(f"📊 Backup Status: {stats}")

                # MEM-01: periodic housekeeping — drop the per-folder backup-info cache
                # (the main unbounded-growth point for a large library) and ask the
                # allocator to release freed memory back to the OS, so RSS doesn't stay
                # pinned at the peak from a large initial scan for the rest of the process's
                # life.
                self.file_listener.clear_caches()
                gc.collect()
                _trim_memory()

                # RELIAB-01: periodic full rescan safety net — see module docstring above.
                # Checked once per status-loop iteration (~every 5 min), so the actual
                # trigger can lag the exact 12h mark by up to that long; negligible.
                await self._maybe_run_periodic_rescan()

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

    async def _maybe_run_periodic_rescan(self) -> None:
        """RELIAB-01: run a full rescan if FULL_RESCAN_INTERVAL_SECONDS have elapsed.

        Extracted as its own method (rather than inlined in the status loop) specifically so
        it's independently testable — patching time.monotonic() through the whole start()
        flow also intercepts asyncio's own internal use of time.monotonic() for scheduling,
        making call counts unpredictable.
        """
        if time.monotonic() - self._last_full_scan_monotonic >= FULL_RESCAN_INTERVAL_SECONDS:
            logger.info("🔄 Starting periodic full rescan (sole sync mechanism — no real-time watcher)")
            await self.file_listener.scan_all_folders()
            self._last_full_scan_monotonic = time.monotonic()
            logger.info(f"🔄 Periodic full rescan completed: {self.file_listener.get_statistics()}")

    async def shutdown(self) -> None:
        """Shutdown the application (ASYNC-06): drain in-flight uploads, close S3."""
        if self._shutdown_called:
            # shutdown may be triggered by the signal handler AND by the main-loop finally clause;
            # the dedicated flag (not self.running) prevents double-cleanup even during init.
            return
        self._shutdown_called = True

        logger.info("Shutting down AWS Copier...")
        self.running = False

        try:
            # Step 1 (ASYNC-06): drain in-flight uploads for up to 60s (D-03).
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

            # Step 2: close S3 client.
            await self.s3_manager.close()
            logger.info("✅ S3 Manager closed")

            # Step 3: shut down the web dashboard last so final log lines are visible.
            if self.web_dashboard is not None:
                await self.web_dashboard.stop()
                logger.info("✅ Web dashboard stopped")

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
    _disable_quickedit_windows()
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
