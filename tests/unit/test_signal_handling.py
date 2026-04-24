"""Behaviour-proving tests for ASYNC-06 signal handling + 60-second drain in main.AWSCopierApp."""

import asyncio
import signal
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main as main_module  # noqa: F401  (imported to confirm module loads)
from main import AWSCopierApp


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Create an AWSCopierApp with mocked S3Manager, FileListener, FolderWatcher.

    The real FileListener is replaced by a MagicMock whose `_active_upload_tasks`
    attribute is a real `set[asyncio.Task]` (the code under test reads this set directly).
    """
    # Write a minimal config.yaml so load_config does not prompt or create defaults.
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "aws_access_key_id: testing\n"
        "aws_secret_access_key: testing\n"
        "aws_region: us-east-1\n"
        "s3_bucket: bucket\n"
        "s3_prefix: ''\n"
        f"watch_folders:\n  {tmp_path}: tmp\n"
        "max_concurrent_uploads: 10\n"
    )
    monkeypatch.chdir(tmp_path)

    with (
        patch("main.S3Manager") as mock_s3_cls,
        patch("main.FileListener") as mock_fl_cls,
        patch("main.FolderWatcher") as mock_fw_cls,
    ):
        mock_s3 = AsyncMock()
        mock_fl = MagicMock()
        mock_fl._active_upload_tasks = set()
        mock_fw = AsyncMock()
        mock_s3_cls.return_value = mock_s3
        mock_fl_cls.return_value = mock_fl
        mock_fw_cls.return_value = mock_fw

        instance = AWSCopierApp()
        instance.running = True  # simulate post-start state
        instance.shutdown_event = asyncio.Event()
        yield instance


class TestHandleSignal:
    """ASYNC-06: _handle_signal flips running and wakes the main loop."""

    async def test_handle_signal_sets_running_false_and_event(self, app):
        assert app.running is True
        assert not app.shutdown_event.is_set()
        await app._handle_signal(signal.SIGTERM)
        assert app.running is False
        assert app.shutdown_event.is_set()


class TestSetupSignalHandlers:
    """ASYNC-06: platform-aware handler registration."""

    async def test_signal_handlers_registered_on_unix(self, app):
        if sys.platform == "win32":
            pytest.skip("Unix-specific test")
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler") as mock_add:
            app._setup_signal_handlers()
        assert mock_add.call_count == 2  # SIGTERM + SIGINT
        registered_signals = {call.args[0] for call in mock_add.call_args_list}
        assert signal.SIGTERM in registered_signals
        assert signal.SIGINT in registered_signals

    async def test_signal_handlers_registered_on_windows(self, app):
        """Simulate Windows by patching sys.platform; signal.signal must be the dispatch."""
        with (
            patch.object(sys, "platform", "win32"),
            patch("main.signal.signal") as mock_signal,
        ):
            app._setup_signal_handlers()
        # Both SIGINT and SIGTERM registered (SIGTERM may fail on older Windows, but install should be attempted)
        assert mock_signal.call_count >= 1
        registered = {call.args[0] for call in mock_signal.call_args_list}
        assert signal.SIGINT in registered


class TestShutdownDrain:
    """ASYNC-06: drain waits up to 60s for FileListener._active_upload_tasks."""

    async def test_drain_waits_for_fast_uploads(self, app, caplog):
        """D-03: three fast uploads complete before the 60s window; no abandonment warning."""
        caplog.set_level("INFO")

        async def fast_upload():
            await asyncio.sleep(0.05)
            return ("file", True)

        # Create 3 real asyncio.Tasks tracked in the mocked FileListener's set.
        tasks = {asyncio.create_task(fast_upload(), name=f"upload-fast-{i}") for i in range(3)}
        app.file_listener._active_upload_tasks = tasks

        await app.shutdown()

        assert all(t.done() for t in tasks)
        assert not any("Abandoned in-flight upload" in rec.message for rec in caplog.records)

    async def test_drain_times_out_and_warns_and_cancels(self, app, caplog, monkeypatch):
        """D-03 + D-04: a slow upload exceeding the 60s window triggers a warning and task.cancel."""
        caplog.set_level("WARNING")

        async def slow_upload():
            try:
                await asyncio.sleep(120)
            except asyncio.CancelledError:
                raise

            return ("file", False)

        slow_task = asyncio.create_task(slow_upload(), name="upload-slow/huge.bin")
        app.file_listener._active_upload_tasks = {slow_task}

        # Patch asyncio.wait to return immediately as timed-out to keep the test fast.
        async def fast_timeout_wait(aws, timeout=None):
            # Return (done=empty, pending=all aws) to simulate 60s timeout expired.
            return set(), set(aws)

        monkeypatch.setattr("main.asyncio.wait", fast_timeout_wait)

        await app.shutdown()

        # Warning logged with the task name
        warning_msgs = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
        assert any("Abandoned in-flight upload" in m and "upload-slow/huge.bin" in m for m in warning_msgs)

        # Clean up the task
        slow_task.cancel()
        try:
            await slow_task
        except (asyncio.CancelledError, Exception):
            pass

    async def test_drain_skips_when_no_active_uploads(self, app, caplog, monkeypatch):
        """Pitfall 3 guard: empty upload set must NOT call asyncio.wait (would raise ValueError)."""
        caplog.set_level("INFO")
        app.file_listener._active_upload_tasks = set()

        with patch("main.asyncio.wait") as mock_wait:
            await app.shutdown()

        assert mock_wait.call_count == 0
        assert any("No in-flight uploads to drain" in rec.message for rec in caplog.records)

    async def test_shutdown_calls_folder_watcher_stop_and_s3_close(self, app):
        """Regression: the existing shutdown sequence (stop watcher, close S3) is preserved."""
        app.file_listener._active_upload_tasks = set()
        await app.shutdown()
        app.folder_watcher.stop.assert_awaited_once()
        app.s3_manager.close.assert_awaited_once()

    async def test_shutdown_is_idempotent(self, app):
        """Calling shutdown twice (signal handler + finally) must short-circuit the second call."""
        app.file_listener._active_upload_tasks = set()
        await app.shutdown()
        # running was flipped to False; second call should return early.
        assert app.running is False
        await app.shutdown()
        app.folder_watcher.stop.assert_awaited_once()  # still only called once total
