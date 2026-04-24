"""
Comprehensive tests for FolderWatcher with proper mocking.
Tests the real-time file monitoring functionality without testing FileListener operations.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from watchdog.events import FileCreatedEvent, FileModifiedEvent, DirCreatedEvent

import pytest

from aws_copier.core.folder_watcher import FolderWatcher, FileChangeHandler
from aws_copier.models.simple_config import SimpleConfig


@pytest.fixture
def temp_watch_folder():
    """Create a temporary folder structure for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Create test files
        (temp_path / "file1.txt").write_text("Content of file 1")
        (temp_path / "file2.txt").write_text("Content of file 2")

        # Create subdirectory with files
        subdir = temp_path / "subdir"
        subdir.mkdir()
        (subdir / "file3.txt").write_text("Content of file 3")

        yield temp_path


@pytest.fixture
def test_config(temp_watch_folder):
    """Test configuration with temporary watch folder."""
    return SimpleConfig(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        s3_prefix="backup",
        watch_folders=[str(temp_watch_folder)],
    )


@pytest.fixture
def mock_file_listener():
    """Create a properly mocked FileListener."""
    mock = AsyncMock()
    mock._process_current_folder = AsyncMock()
    return mock


@pytest.fixture
def folder_watcher(test_config, mock_file_listener):
    """FolderWatcher with mocked FileListener for isolated testing."""
    return FolderWatcher(test_config, mock_file_listener)


@pytest.fixture
def mock_event_loop():
    """Create a mock event loop for testing."""
    loop = MagicMock()

    # Mock call_soon_threadsafe to avoid creating actual coroutines
    def mock_call_soon_threadsafe(func, coro):
        # Don't actually call the function, just record that it was called
        pass

    loop.call_soon_threadsafe = MagicMock(side_effect=mock_call_soon_threadsafe)
    return loop


class TestFolderWatcherCore:
    """Test core FolderWatcher functionality."""

    def test_folder_watcher_initialization(self, folder_watcher, test_config, mock_file_listener):
        """Test FolderWatcher initialization with proper configuration."""
        assert folder_watcher.config == test_config
        assert folder_watcher.file_listener == mock_file_listener
        assert folder_watcher.observer is not None
        assert folder_watcher.running is False
        assert folder_watcher.event_loop is None
        assert len(folder_watcher.handlers) == 0

    async def test_start_folder_watcher(self, folder_watcher, temp_watch_folder):
        """Test starting the folder watcher."""
        with patch.object(folder_watcher.observer, "start") as mock_observer_start:
            await folder_watcher.start()

            # Should be running
            assert folder_watcher.running is True
            assert folder_watcher.event_loop is not None

            # Should have created handlers for watch folders
            assert len(folder_watcher.handlers) == 1
            assert str(temp_watch_folder) in folder_watcher.handlers

            # Observer should be started
            mock_observer_start.assert_called_once()

    async def test_stop_folder_watcher(self, folder_watcher):
        """Test stopping the folder watcher."""
        # Start first
        with patch.object(folder_watcher.observer, "start"):
            await folder_watcher.start()

        # Now stop
        with (
            patch.object(folder_watcher.observer, "stop") as mock_observer_stop,
            patch.object(folder_watcher.observer, "join") as mock_observer_join,
        ):
            await folder_watcher.stop()

            # Should not be running
            assert folder_watcher.running is False

            # Observer should be stopped
            mock_observer_stop.assert_called_once()
            mock_observer_join.assert_called_once()

    async def test_start_already_running(self, folder_watcher):
        """Test starting folder watcher when already running."""
        # Start first time
        with patch.object(folder_watcher.observer, "start"):
            await folder_watcher.start()

        # Try to start again
        with patch.object(folder_watcher.observer, "start") as mock_observer_start:
            await folder_watcher.start()

            # Observer start should not be called again
            mock_observer_start.assert_not_called()

    async def test_stop_not_running(self, folder_watcher):
        """Test stopping folder watcher when not running."""
        with patch.object(folder_watcher.observer, "stop") as mock_observer_stop:
            await folder_watcher.stop()

            # Observer stop should not be called
            mock_observer_stop.assert_not_called()


class TestFolderWatcherStatistics:
    """Test FolderWatcher statistics and monitoring."""

    def test_get_statistics(self, folder_watcher):
        """Test getting watcher statistics."""
        stats = folder_watcher.get_statistics()

        assert "watched_folders" in stats
        assert "events_processed" in stats
        assert "files_processed" in stats
        assert "running" in stats
        assert "observer_threads" in stats

        assert stats["running"] is False
        assert stats["watched_folders"] == 0

    async def test_statistics_after_start(self, folder_watcher):
        """Test statistics after starting watcher."""
        with patch.object(folder_watcher.observer, "start"):
            await folder_watcher.start()

        stats = folder_watcher.get_statistics()

        assert stats["running"] is True
        assert stats["watched_folders"] == 1

    def test_is_running(self, folder_watcher):
        """Test is_running method."""
        assert folder_watcher.is_running() is False

        folder_watcher.running = True
        assert folder_watcher.is_running() is True


class TestFileChangeHandler:
    """Test FileChangeHandler functionality."""

    @pytest.fixture
    def file_change_handler(self, test_config, temp_watch_folder, mock_file_listener, mock_event_loop):
        """Create a FileChangeHandler for testing."""
        return FileChangeHandler(test_config, temp_watch_folder, mock_file_listener, mock_event_loop)

    def test_file_change_handler_initialization(
        self, file_change_handler, test_config, temp_watch_folder, mock_file_listener, mock_event_loop
    ):
        """Test FileChangeHandler initialization."""
        assert file_change_handler.config == test_config
        assert file_change_handler.watch_folder == temp_watch_folder
        assert file_change_handler.file_listener == mock_file_listener
        assert file_change_handler.event_loop == mock_event_loop
        # IGNORE-03: no local ignore_patterns — all ignore logic delegated to IGNORE_RULES singleton
        assert not hasattr(file_change_handler, "ignore_patterns")

    def test_on_any_event_uses_run_coroutine_threadsafe(self, file_change_handler, temp_watch_folder):
        """ASYNC-01: watchdog events are bridged via asyncio.run_coroutine_threadsafe, not call_soon_threadsafe.

        Proves the fix for the Python 3.10+ bug where call_soon_threadsafe(create_task, coro)
        silently dropped the coroutine. After the fix, every event scheduled must use
        asyncio.run_coroutine_threadsafe, which returns a concurrent.futures.Future.
        """
        test_file = temp_watch_folder / "new_file.txt"
        test_file.write_text("new content")
        event = FileCreatedEvent(str(test_file))

        with (
            patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe") as mock_run,
            patch.object(file_change_handler, "_process_changed_file") as mock_process,
        ):
            # mock_process returns a coroutine object when called
            mock_process.return_value = MagicMock(name="coroutine")
            file_change_handler.on_any_event(event)

        # Assert: the new API was used exactly once with the handler's event_loop
        assert mock_run.call_count == 1
        args, _ = mock_run.call_args
        assert args[1] is file_change_handler.event_loop

    def test_on_any_event_does_not_use_call_soon_threadsafe(self, file_change_handler, temp_watch_folder):
        """ASYNC-01 regression guard: the old broken bridge must never be reintroduced."""
        test_file = temp_watch_folder / "new_file.txt"
        test_file.write_text("new content")
        event = FileCreatedEvent(str(test_file))

        with (
            patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe"),
            patch.object(file_change_handler, "_process_changed_file"),
        ):
            file_change_handler.on_any_event(event)

        # The handler's event_loop is a MagicMock in the fixture; assert call_soon_threadsafe was NOT invoked.
        assert file_change_handler.event_loop.call_soon_threadsafe.call_count == 0

    def test_on_any_event_skips_ignored_file_via_ignore_rules(self, file_change_handler, temp_watch_folder):
        """IGNORE-03: FileChangeHandler uses IGNORE_RULES.should_ignore_file, not its own local set.

        Creates a .env file (which must be ignored by IGNORE_RULES) and asserts that
        run_coroutine_threadsafe is NOT called for it.
        """
        ignored_file = temp_watch_folder / ".env"
        ignored_file.write_text("SECRET=x")
        event = FileCreatedEvent(str(ignored_file))

        with patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe") as mock_run:
            file_change_handler.on_any_event(event)

        assert mock_run.call_count == 0

    def test_on_any_event_file_modified(self, file_change_handler, temp_watch_folder):
        """Test handling file modified events."""
        test_file = temp_watch_folder / "file1.txt"

        event = FileModifiedEvent(str(test_file))

        # Mock _process_changed_file to avoid coroutine creation
        with (
            patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe") as mock_run,
            patch.object(file_change_handler, "_process_changed_file") as mock_process,
        ):
            mock_process.return_value = MagicMock(name="coroutine")
            file_change_handler.on_any_event(event)

            # Verify run_coroutine_threadsafe was called
            assert mock_run.call_count == 1

    def test_on_any_event_directory_created(self, file_change_handler, temp_watch_folder):
        """Test handling directory created events (should be ignored)."""
        test_dir = temp_watch_folder / "new_dir"
        test_dir.mkdir()

        event = DirCreatedEvent(str(test_dir))

        with patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe") as mock_run:
            # Should not schedule processing for directories
            file_change_handler.on_any_event(event)

            # Verify run_coroutine_threadsafe was not called
            assert mock_run.call_count == 0

    def test_on_any_event_ignored_file(self, file_change_handler, temp_watch_folder):
        """Test handling events for ignored files."""
        ignored_file = temp_watch_folder / ".DS_Store"
        ignored_file.write_text("System file")

        event = FileCreatedEvent(str(ignored_file))

        with patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe") as mock_run:
            # Should not schedule processing for ignored files
            file_change_handler.on_any_event(event)

            # Verify run_coroutine_threadsafe was not called
            assert mock_run.call_count == 0

    def test_on_any_event_backup_info_file(self, file_change_handler, temp_watch_folder):
        """Test handling events for .milo_backup.info files (should be ignored)."""
        backup_file = temp_watch_folder / ".milo_backup.info"
        backup_file.write_text('{"files": {}}')

        event = FileModifiedEvent(str(backup_file))

        with patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe") as mock_run:
            # Should not schedule processing for backup info files
            file_change_handler.on_any_event(event)

            # Verify run_coroutine_threadsafe was not called
            assert mock_run.call_count == 0

    def test_on_any_event_nonexistent_file(self, file_change_handler, temp_watch_folder):
        """Test handling events for files that don't exist."""
        nonexistent_file = temp_watch_folder / "nonexistent.txt"

        event = FileCreatedEvent(str(nonexistent_file))

        with patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe") as mock_run:
            # Should not schedule processing for nonexistent files
            file_change_handler.on_any_event(event)

            # Verify run_coroutine_threadsafe was not called
            assert mock_run.call_count == 0

    async def test_process_changed_file_in_watch_folder(self, file_change_handler, temp_watch_folder):
        """Test processing a changed file within watch folder."""
        test_file = temp_watch_folder / "test_file.txt"

        await file_change_handler._process_changed_file(test_file, "created")

        # Should call file_listener._process_current_folder with the parent folder
        file_change_handler.file_listener._process_current_folder.assert_called_once_with(temp_watch_folder)

    async def test_process_changed_file_in_subfolder(self, file_change_handler, temp_watch_folder):
        """Test processing a changed file in a subfolder."""
        test_file = temp_watch_folder / "subdir" / "test_file.txt"

        await file_change_handler._process_changed_file(test_file, "modified")

        # Should call file_listener._process_current_folder with the subfolder
        expected_folder = temp_watch_folder / "subdir"
        file_change_handler.file_listener._process_current_folder.assert_called_once_with(expected_folder)

    async def test_process_changed_file_outside_watch_folder(self, file_change_handler):
        """Test processing a changed file outside watch folders."""
        outside_file = Path("/tmp/outside_file.txt")

        await file_change_handler._process_changed_file(outside_file, "created")

        # Should not call file_listener._process_current_folder
        file_change_handler.file_listener._process_current_folder.assert_not_called()


class TestFolderWatcherIntegration:
    """Test FolderWatcher integration scenarios."""

    async def test_add_folder_watch_success(self, folder_watcher, temp_watch_folder):
        """Test successfully adding a folder to watch."""
        # Set up event loop
        folder_watcher.event_loop = asyncio.get_running_loop()

        with patch.object(folder_watcher.observer, "schedule") as mock_schedule:
            await folder_watcher._add_folder_watch(temp_watch_folder)

            # Should have created a handler and scheduled it
            mock_schedule.assert_called_once()
            assert str(temp_watch_folder) in folder_watcher.handlers

    async def test_add_folder_watch_nonexistent(self, folder_watcher):
        """Test adding a non-existent folder to watch."""
        nonexistent_folder = Path("/nonexistent/folder")

        # Set up event loop
        folder_watcher.event_loop = asyncio.get_running_loop()

        with patch.object(folder_watcher.observer, "schedule") as mock_schedule:
            await folder_watcher._add_folder_watch(nonexistent_folder)

            # Should not schedule anything for non-existent folder
            mock_schedule.assert_not_called()
            assert str(nonexistent_folder) not in folder_watcher.handlers

    async def test_add_folder_watch_file_not_directory(self, folder_watcher, temp_watch_folder):
        """Test adding a file (not directory) to watch."""
        test_file = temp_watch_folder / "file1.txt"

        # Set up event loop
        folder_watcher.event_loop = asyncio.get_running_loop()

        with patch.object(folder_watcher.observer, "schedule") as mock_schedule:
            await folder_watcher._add_folder_watch(test_file)

            # Should not schedule anything for files
            mock_schedule.assert_not_called()
            assert str(test_file) not in folder_watcher.handlers

    async def test_add_folder_watch_no_event_loop(self, folder_watcher, temp_watch_folder):
        """Test adding folder watch without event loop."""
        # Don't set event loop
        folder_watcher.event_loop = None

        with pytest.raises(RuntimeError, match="Event loop not available"):
            await folder_watcher._add_folder_watch(temp_watch_folder)


class TestFolderWatcherErrorHandling:
    """Test FolderWatcher error handling."""

    def test_file_change_handler_error_handling(
        self, test_config, temp_watch_folder, mock_file_listener, mock_event_loop
    ):
        """Test FileChangeHandler handles errors gracefully."""
        file_change_handler = FileChangeHandler(test_config, temp_watch_folder, mock_file_listener, mock_event_loop)

        # Create an event for a file that will cause an error
        test_file = temp_watch_folder / "error_file.txt"
        event = FileCreatedEvent(str(test_file))

        # Mock run_coroutine_threadsafe to raise an exception
        with patch("aws_copier.core.folder_watcher.asyncio.run_coroutine_threadsafe") as mock_run:
            mock_run.side_effect = Exception("Test error")

            # Should not raise an exception
            try:
                file_change_handler.on_any_event(event)
            except Exception as e:
                pytest.fail(f"FileChangeHandler should handle errors gracefully, but raised: {e}")

    async def test_process_changed_file_error_handling(
        self, test_config, temp_watch_folder, mock_file_listener, mock_event_loop
    ):
        """Test _process_changed_file handles errors gracefully."""
        file_change_handler = FileChangeHandler(test_config, temp_watch_folder, mock_file_listener, mock_event_loop)

        # Mock file_listener to raise an exception
        file_change_handler.file_listener._process_current_folder.side_effect = Exception("Test error")

        test_file = temp_watch_folder / "test_file.txt"

        # Should not raise an exception
        try:
            await file_change_handler._process_changed_file(test_file, "created")
        except Exception as e:
            pytest.fail(f"_process_changed_file should handle errors gracefully, but raised: {e}")


class TestFolderWatcherConfiguration:
    """Test FolderWatcher configuration scenarios."""

    def test_multiple_watch_folders(self, mock_file_listener):
        """Test FolderWatcher with multiple watch folders."""
        with tempfile.TemporaryDirectory() as temp_dir1, tempfile.TemporaryDirectory() as temp_dir2:
            config = SimpleConfig(
                watch_folders=[temp_dir1, temp_dir2],
            )

            FolderWatcher(config, mock_file_listener)

            assert len(config.watch_folders) == 2

    def test_empty_watch_folders(self, mock_file_listener):
        """Test FolderWatcher with empty watch folders list."""
        config = SimpleConfig(watch_folders=[])
        FolderWatcher(config, mock_file_listener)

        assert len(config.watch_folders) == 0

    async def test_start_with_empty_watch_folders(self, mock_file_listener):
        """Test starting watcher with no folders to watch."""
        config = SimpleConfig(watch_folders=[])
        watcher = FolderWatcher(config, mock_file_listener)

        with patch.object(watcher.observer, "start") as mock_observer_start:
            await watcher.start()

            # Should still start but with no handlers
            assert watcher.running is True
            assert len(watcher.handlers) == 0
            mock_observer_start.assert_called_once()
