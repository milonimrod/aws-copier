"""Tests for queue manager."""

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from aws_copier.core.queue_manager import QueueManager
from aws_copier.models.simple_config import SimpleConfig


@pytest.fixture
def temp_config():
    """Create a temporary config for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config = SimpleConfig(
            batch_folder=str(Path(temp_dir) / "batches"),
            batch_save_interval=1  # Faster for testing
        )
        yield config


@pytest_asyncio.fixture
async def queue_manager(temp_config):
    """Create a queue manager for testing."""
    manager = QueueManager(temp_config)
    await manager.start()
    yield manager
    await manager.stop()


@pytest.mark.asyncio
async def test_queue_manager_initialization(temp_config):
    """Test queue manager initialization."""
    manager = QueueManager(temp_config)

    assert manager.config == temp_config
    assert manager.batch_folder == temp_config.batch_folder
    assert not manager._running

    await manager.start()
    assert manager._running

    await manager.stop()
    assert not manager._running


@pytest.mark.asyncio
async def test_add_single_file(queue_manager):
    """Test adding a single file to the queue."""
    test_file = Path("/tmp/test_file.txt")

    await queue_manager.add_file(test_file)

    stats = queue_manager.get_statistics()
    assert stats["current_batch_size"] == 1
    assert stats["files_added"] == 1


@pytest.mark.asyncio
async def test_add_multiple_files(queue_manager):
    """Test adding multiple files to the queue."""
    test_files = [
        Path("/tmp/file1.txt"),
        Path("/tmp/file2.txt"),
        Path("/tmp/file3.txt")
    ]

    await queue_manager.add_files(test_files)

    stats = queue_manager.get_statistics()
    assert stats["current_batch_size"] == 3
    assert stats["files_added"] == 3


@pytest.mark.asyncio
async def test_duplicate_files_ignored(queue_manager):
    """Test that duplicate files are ignored."""
    test_file = Path("/tmp/test_file.txt")

    # Add the same file twice
    await queue_manager.add_file(test_file)
    await queue_manager.add_file(test_file)

    stats = queue_manager.get_statistics()
    assert stats["current_batch_size"] == 1  # Only one file should be in batch
    assert stats["files_added"] == 1  # Only one file should be counted


@pytest.mark.asyncio
async def test_batch_save_functionality(queue_manager):
    """Test that batches are saved to files."""
    test_files = [
        Path("/tmp/file1.txt"),
        Path("/tmp/file2.txt")
    ]

    await queue_manager.add_files(test_files)

    # Force save
    await queue_manager.force_save()

    # Check that batch file was created
    batch_files = queue_manager.get_batch_files()
    assert len(batch_files) == 1

    # Check batch file content
    batch_data = queue_manager.load_batch_file(batch_files[0])
    assert batch_data is not None
    assert batch_data["file_count"] == 2
    assert len(batch_data["files"]) == 2

    # Current batch should be empty after save
    stats = queue_manager.get_statistics()
    assert stats["current_batch_size"] == 0


@pytest.mark.asyncio
async def test_get_total_queued_files(queue_manager):
    """Test getting total queued files count."""
    # Add files to current batch
    test_files = [Path("/tmp/file1.txt"), Path("/tmp/file2.txt")]
    await queue_manager.add_files(test_files)

    # Save current batch
    await queue_manager.force_save()

    # Add more files to new batch
    await queue_manager.add_file(Path("/tmp/file3.txt"))

    total_files = queue_manager.get_total_queued_files()
    assert total_files == 3  # 2 in saved batch + 1 in current batch


@pytest.mark.asyncio
async def test_delete_batch_file(queue_manager):
    """Test deleting batch files."""
    # Add files and save
    await queue_manager.add_file(Path("/tmp/test_file.txt"))
    await queue_manager.force_save()

    batch_files = queue_manager.get_batch_files()
    assert len(batch_files) == 1

    # Delete batch file
    success = queue_manager.delete_batch_file(batch_files[0])
    assert success

    # Verify file was deleted
    batch_files = queue_manager.get_batch_files()
    assert len(batch_files) == 0


def test_get_statistics_structure(temp_config):
    """Test that statistics have the expected structure."""
    manager = QueueManager(temp_config)
    stats = manager.get_statistics()

    expected_keys = [
        "current_batch_size",
        "total_queued_files",
        "batch_files_count",
        "running",
        "files_added",
        "batches_saved",
        "last_save_time"
    ]

    for key in expected_keys:
        assert key in stats
