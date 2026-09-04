"""
Comprehensive tests for FileListener with proper S3Manager mocking.
Tests the incremental backup functionality without testing S3 operations.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aws_copier.core.file_listener import FileListener
from aws_copier.core.s3_manager import estimate_upload_timeout
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
        (subdir / "file4.txt").write_text("Content of file 4")

        # Create nested subdirectory
        nested_dir = subdir / "nested"
        nested_dir.mkdir()
        (nested_dir / "file5.txt").write_text("Content of file 5")

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
def mock_s3_manager():
    """Create a properly mocked S3Manager."""
    mock = AsyncMock()
    mock.upload_file.return_value = True
    mock.check_exists.return_value = False
    return mock


@pytest.fixture
def file_listener(test_config, mock_s3_manager):
    """FileListener with mocked S3Manager for isolated testing."""
    return FileListener(test_config, mock_s3_manager)


class TestFileListenerCore:
    """Test core FileListener functionality."""

    async def test_scan_all_folders_initial(self, file_listener, temp_watch_folder):
        """Test initial scan creates backup info files."""
        # Run initial scan
        await file_listener.scan_all_folders()

        # Check that backup info files were created
        root_info = temp_watch_folder / ".milo_backup.info"
        subdir_info = temp_watch_folder / "subdir" / ".milo_backup.info"
        nested_info = temp_watch_folder / "subdir" / "nested" / ".milo_backup.info"

        assert root_info.exists()
        assert subdir_info.exists()
        assert nested_info.exists()

        # Verify root folder backup info
        with open(root_info) as f:
            root_data = json.load(f)

        assert "timestamp" in root_data
        assert "files" in root_data
        assert "file1.txt" in root_data["files"]
        assert "file2.txt" in root_data["files"]
        assert len(root_data["files"]) == 2  # Only files in root, not subdirs

    async def test_backup_info_contains_correct_md5(self, file_listener, temp_watch_folder):
        """Test that backup info files contain correct MD5 hashes."""
        # Run initial scan
        await file_listener.scan_all_folders()

        # Check MD5 in backup info
        root_info = temp_watch_folder / ".milo_backup.info"
        with open(root_info) as f:
            root_data = json.load(f)

        # Calculate expected MD5 for file1.txt
        import hashlib

        expected_md5 = hashlib.md5("Content of file 1".encode()).hexdigest()

        # New format: each entry is a {md5, mtime} dict
        assert root_data["files"]["file1.txt"]["md5"] == expected_md5

    async def test_incremental_scan_skips_unchanged_files(self, file_listener, temp_watch_folder):
        """Test that unchanged files are skipped on subsequent scans."""
        # Run initial scan
        await file_listener.scan_all_folders()

        # Reset mock call counts
        file_listener.s3_manager.upload_file.reset_mock()

        # Run second scan (no files changed)
        await file_listener.scan_all_folders()

        # Should not upload any files since nothing changed
        file_listener.s3_manager.upload_file.assert_not_called()

    async def test_incremental_scan_detects_changed_files(self, file_listener, temp_watch_folder):
        """Test that changed files are detected and uploaded."""
        # Run initial scan
        await file_listener.scan_all_folders()

        # Modify a file
        (temp_watch_folder / "file1.txt").write_text("Modified content of file 1")

        # Reset mock call counts
        file_listener.s3_manager.upload_file.reset_mock()

        # Run second scan
        await file_listener.scan_all_folders()

        # Should upload the changed file
        file_listener.s3_manager.upload_file.assert_called()

        # Verify the correct file was uploaded
        upload_calls = file_listener.s3_manager.upload_file.call_args_list
        uploaded_files = [call[0][0].name for call in upload_calls]
        assert "file1.txt" in uploaded_files

    async def test_incremental_scan_detects_new_files(self, file_listener, temp_watch_folder):
        """Test that new files are detected and uploaded."""
        # Run initial scan
        await file_listener.scan_all_folders()

        # Add a new file
        (temp_watch_folder / "new_file.txt").write_text("Content of new file")

        # Reset mock call counts
        file_listener.s3_manager.upload_file.reset_mock()

        # Run second scan
        await file_listener.scan_all_folders()

        # Should upload the new file
        file_listener.s3_manager.upload_file.assert_called()

        # Verify the new file was uploaded
        upload_calls = file_listener.s3_manager.upload_file.call_args_list
        uploaded_files = [call[0][0].name for call in upload_calls]
        assert "new_file.txt" in uploaded_files


class TestScanStatistics:
    """STATS-01: scan_all_folders resets stats each call; skipped_files counts once per file."""

    async def test_scan_all_folders_resets_stats_at_start(self, file_listener, temp_watch_folder):
        """A second scan's stats reflect only that scan, not an accumulating total."""
        await file_listener.scan_all_folders()
        first_scan_uploaded = file_listener.get_statistics()["uploaded_files"]
        assert first_scan_uploaded > 0  # sanity: fixture files did get uploaded

        # Second scan: nothing changed, so uploaded_files must reset to 0, not stay >0.
        await file_listener.scan_all_folders()
        second_scan_stats = file_listener.get_statistics()
        assert second_scan_stats["uploaded_files"] == 0
        assert second_scan_stats["scanned_folders"] > 0  # this scan did run, just uploaded nothing

    async def test_skipped_files_counted_once_per_unchanged_file(self, file_listener, temp_watch_folder):
        """An unchanged file (mtime-cache hit on the second scan) increments skipped_files by
        exactly 1, not 2 — regression for double-counting between the mtime-skip fast path in
        _scan_current_files and the definitive skip decision in _determine_files_to_upload."""
        await file_listener.scan_all_folders()  # first scan: everything is new, nothing skipped yet

        await file_listener.scan_all_folders()  # second scan: all fixture files unchanged
        stats = file_listener.get_statistics()
        # temp_watch_folder fixture has 5 files total (2 root + 2 subdir + 1 nested), all
        # hitting the mtime-cache fast path on this second scan.
        assert stats["skipped_files"] == 5


class TestFileListenerOperations:
    """Test specific FileListener operations."""

    async def test_process_current_folder(self, file_listener, temp_watch_folder):
        """Test processing a specific folder."""
        folder_path = temp_watch_folder / "subdir"

        # Process just the subdirectory
        await file_listener._process_current_folder(folder_path)

        # Check that backup info was created for this folder
        backup_info = folder_path / ".milo_backup.info"
        assert backup_info.exists()

        # Verify content
        with open(backup_info) as f:
            data = json.load(f)

        assert "file3.txt" in data["files"]
        assert "file4.txt" in data["files"]

    async def test_scan_current_files(self, file_listener, temp_watch_folder):
        """Test scanning files in a folder — returns {md5, mtime} dicts (new format)."""
        folder_path = temp_watch_folder

        current_files = await file_listener._scan_current_files(folder_path)

        assert "file1.txt" in current_files
        assert "file2.txt" in current_files
        assert len(current_files) == 2  # Should not include subdirectories

        # Verify MD5 calculation — new format wraps md5 in a dict
        import hashlib

        expected_md5 = hashlib.md5("Content of file 1".encode()).hexdigest()
        assert current_files["file1.txt"]["md5"] == expected_md5
        assert "mtime" in current_files["file1.txt"]

    async def test_determine_files_to_upload_new_files(self, file_listener, temp_watch_folder):
        """Test determining which files need upload when all are new."""
        current_files = {"file1.txt": "md5_hash_1", "file2.txt": "md5_hash_2"}
        existing_backup_info = {}

        files_to_upload, files_to_move = file_listener._determine_files_to_upload(
            current_files, existing_backup_info, temp_watch_folder
        )

        assert set(files_to_upload) == {"file1.txt", "file2.txt"}
        assert files_to_move == []

    async def test_determine_files_to_upload_changed_files(self, file_listener, temp_watch_folder):
        """Test determining which files need upload when some have changed."""
        current_files = {"file1.txt": "new_md5_hash_1", "file2.txt": "md5_hash_2"}
        existing_backup_info = {"file1.txt": "old_md5_hash_1", "file2.txt": "md5_hash_2"}

        files_to_upload, files_to_move = file_listener._determine_files_to_upload(
            current_files, existing_backup_info, temp_watch_folder
        )

        assert files_to_upload == ["file1.txt"]  # Only changed file
        assert files_to_move == []

    async def test_determine_files_to_upload_no_changes(self, file_listener, temp_watch_folder):
        """Test determining which files need upload when nothing changed."""
        current_files = {"file1.txt": "md5_hash_1", "file2.txt": "md5_hash_2"}
        existing_backup_info = {"file1.txt": "md5_hash_1", "file2.txt": "md5_hash_2"}

        files_to_upload, files_to_move = file_listener._determine_files_to_upload(
            current_files, existing_backup_info, temp_watch_folder
        )

        assert files_to_upload == []
        assert files_to_move == []

    async def test_determine_files_to_upload_detects_moved_file(self, file_listener, temp_watch_folder):
        """MOVE-01: unchanged content at a stale recorded s3_key is flagged as a move, not an upload."""
        current_files = {"file1.txt": {"md5": "same_md5", "mtime": 123.0}}
        existing_backup_info = {
            "file1.txt": {"md5": "same_md5", "mtime": 123.0, "s3_key": "OldFolderName/file1.txt"}
        }

        files_to_upload, files_to_move = file_listener._determine_files_to_upload(
            current_files, existing_backup_info, temp_watch_folder
        )

        assert files_to_upload == []
        assert files_to_move == ["file1.txt"]

    async def test_determine_files_to_upload_legacy_entry_without_s3_key_not_moved(
        self, file_listener, temp_watch_folder
    ):
        """A legacy entry with s3_key=None (post-migration) is trusted, never flagged as a move."""
        current_files = {"file1.txt": {"md5": "same_md5", "mtime": 123.0}}
        existing_backup_info = {"file1.txt": {"md5": "same_md5", "mtime": 123.0, "s3_key": None}}

        files_to_upload, files_to_move = file_listener._determine_files_to_upload(
            current_files, existing_backup_info, temp_watch_folder
        )

        assert files_to_upload == []
        assert files_to_move == []


class TestFileListenerMoveReconciliation:
    """MOVE-01 end-to-end: renaming a watched subdirectory on disk must reconcile S3, not skip it.

    Regression coverage for the bug where .milo_backup.info travels with a renamed/moved
    directory (since it's a file inside that directory), so its stale per-file MD5 entries
    made the app believe already-synced files were still correctly placed in S3 — even
    though the S3 key computed for their new location had changed and nothing was ever
    uploaded there.
    """

    async def test_renamed_subdirectory_triggers_move_not_reupload(
        self, file_listener, mock_s3_manager, temp_watch_folder
    ):
        old_dir = temp_watch_folder / "album_old"
        old_dir.mkdir()
        (old_dir / "photo.jpg").write_text("photo content")

        # Step 1: initial backup of the directory at its original location.
        await file_listener._process_current_folder(old_dir)
        assert mock_s3_manager.upload_file.await_count == 1

        s3_folder_name = temp_watch_folder.name
        old_s3_key = f"{s3_folder_name}/album_old/photo.jpg"
        new_s3_key = f"{s3_folder_name}/album_new/photo.jpg"

        backup_info = json.loads((old_dir / ".milo_backup.info").read_text())
        assert backup_info["files"]["photo.jpg"]["s3_key"] == old_s3_key

        # Step 2: rename the directory on disk. .milo_backup.info (with the stale s3_key)
        # travels with it; the photo's own mtime is untouched by the rename.
        new_dir = temp_watch_folder / "album_new"
        old_dir.rename(new_dir)

        mock_s3_manager.upload_file.reset_mock()
        mock_s3_manager.check_exists.reset_mock()
        mock_s3_manager.move_object.return_value = True

        # Step 3: scan the directory at its new location.
        await file_listener._process_current_folder(new_dir)

        # Content is unchanged, so this must be reconciled via server-side move, never a re-upload.
        mock_s3_manager.upload_file.assert_not_awaited()
        mock_s3_manager.move_object.assert_awaited_once_with(old_s3_key, new_s3_key)

        updated_backup_info = json.loads((new_dir / ".milo_backup.info").read_text())
        assert updated_backup_info["files"]["photo.jpg"]["s3_key"] == new_s3_key
        assert file_listener.get_statistics()["moved_files"] == 1

    async def test_renamed_subdirectory_reupload_falls_back_when_move_fails(
        self, file_listener, mock_s3_manager, temp_watch_folder
    ):
        """A failed S3-side move leaves the stale s3_key in place so the next scan retries it."""
        old_dir = temp_watch_folder / "album_old"
        old_dir.mkdir()
        (old_dir / "photo.jpg").write_text("photo content")
        await file_listener._process_current_folder(old_dir)

        new_dir = temp_watch_folder / "album_new"
        old_dir.rename(new_dir)

        mock_s3_manager.upload_file.reset_mock()
        mock_s3_manager.move_object.return_value = False  # simulate S3-side failure

        await file_listener._process_current_folder(new_dir)

        s3_folder_name = temp_watch_folder.name
        old_s3_key = f"{s3_folder_name}/album_old/photo.jpg"
        updated_backup_info = json.loads((new_dir / ".milo_backup.info").read_text())
        # Stale entry preserved (not silently marked as synced at the new key) so the next
        # scan's _determine_files_to_upload still detects the mismatch and retries the move.
        assert updated_backup_info["files"]["photo.jpg"]["s3_key"] == old_s3_key


class TestFileListenerDeletionPropagation:
    """DEL-01: a file removed from a watched folder locally must be soft-deleted from S3
    (not silently orphaned), but only when the scan can be trusted and the count of
    apparently-deleted files stays under the safety cap.
    """

    def test_determine_files_to_delete_finds_missing_tracked_files(self, file_listener):
        current_files = {"still_here.jpg": {"md5": "a"}}
        existing_backup_info = {
            "still_here.jpg": {"md5": "a", "s3_key": "k/still_here.jpg"},
            "gone.jpg": {"md5": "b", "s3_key": "k/gone.jpg"},
        }

        result = file_listener._determine_files_to_delete(current_files, existing_backup_info)

        assert result == ["gone.jpg"]

    async def test_deleted_file_is_soft_deleted_and_dropped_from_backup_info(
        self, file_listener, mock_s3_manager, temp_watch_folder
    ):
        folder = temp_watch_folder / "album"
        folder.mkdir()
        (folder / "keep.jpg").write_text("keep")
        (folder / "gone.jpg").write_text("gone")
        await file_listener._process_current_folder(folder)
        assert mock_s3_manager.upload_file.await_count == 2

        s3_folder_name = temp_watch_folder.name
        gone_s3_key = f"{s3_folder_name}/album/gone.jpg"

        (folder / "gone.jpg").unlink()
        mock_s3_manager.upload_file.reset_mock()
        mock_s3_manager.soft_delete_object.return_value = True

        await file_listener._process_current_folder(folder)

        mock_s3_manager.soft_delete_object.assert_awaited_once_with(gone_s3_key)
        updated = json.loads((folder / ".milo_backup.info").read_text())
        assert "gone.jpg" not in updated["files"]
        assert "keep.jpg" in updated["files"]
        assert file_listener.get_statistics()["deleted_files"] == 1

    async def test_failed_soft_delete_keeps_entry_tracked_for_retry(
        self, file_listener, mock_s3_manager, temp_watch_folder
    ):
        folder = temp_watch_folder / "album"
        folder.mkdir()
        (folder / "gone.jpg").write_text("gone")
        await file_listener._process_current_folder(folder)

        (folder / "gone.jpg").unlink()
        mock_s3_manager.upload_file.reset_mock()
        mock_s3_manager.soft_delete_object.return_value = False  # simulate S3-side failure

        await file_listener._process_current_folder(folder)

        updated = json.loads((folder / ".milo_backup.info").read_text())
        assert "gone.jpg" in updated["files"]  # not silently dropped — retried next scan

    async def test_deletion_count_over_cap_is_not_propagated(
        self, file_listener, mock_s3_manager, temp_watch_folder
    ):
        """DEL-01 safety cap: more apparent deletions than max_deletions_per_scan must be
        treated as suspicious, not executed."""
        file_listener.config.max_deletions_per_scan = 2
        folder = temp_watch_folder / "album"
        folder.mkdir()
        filenames = ["a.jpg", "b.jpg", "c.jpg"]
        for name in filenames:
            (folder / name).write_text(name)
        await file_listener._process_current_folder(folder)

        for name in filenames:
            (folder / name).unlink()
        mock_s3_manager.upload_file.reset_mock()
        mock_s3_manager.soft_delete_object.return_value = True

        await file_listener._process_current_folder(folder)

        mock_s3_manager.soft_delete_object.assert_not_awaited()
        updated = json.loads((folder / ".milo_backup.info").read_text())
        assert set(updated["files"].keys()) == set(filenames)
        assert file_listener.get_statistics()["deleted_files"] == 0

    async def test_unreliable_directory_listing_never_triggers_deletion(
        self, file_listener, mock_s3_manager, temp_watch_folder, monkeypatch
    ):
        """DEL-01 core safety property: if the folder can't be reliably listed this cycle,
        nothing must be treated as deleted — even though every tracked file would appear
        "missing" from an (incorrectly) empty scan result."""
        folder = temp_watch_folder / "album"
        folder.mkdir()
        (folder / "photo.jpg").write_text("photo")
        await file_listener._process_current_folder(folder)
        mock_s3_manager.upload_file.reset_mock()

        real_iterdir = Path.iterdir

        def flaky_iterdir(self):
            if self == folder:
                raise OSError("simulated transient listing failure")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", flaky_iterdir)

        await file_listener._process_current_folder(folder)

        mock_s3_manager.soft_delete_object.assert_not_awaited()
        # .milo_backup.info must be completely untouched by the failed scan.
        updated = json.loads((folder / ".milo_backup.info").read_text())
        assert "photo.jpg" in updated["files"]


class TestConcurrentFolderTraversal:
    """CONC-02: sibling folders are processed concurrently, throttled by folder_semaphore."""

    async def test_sibling_folders_process_concurrently(self, file_listener, temp_watch_folder):
        """Multiple sibling folders' _process_current_folder calls overlap, not run one at a time."""
        for i in range(6):
            d = temp_watch_folder / f"event_{i}"
            d.mkdir()
            (d / "photo.jpg").write_text(f"content {i}")

        file_listener.folder_semaphore = asyncio.Semaphore(3)

        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()
        original = file_listener._process_current_folder

        async def instrumented(folder_path):
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.02)
            result = await original(folder_path)
            async with lock:
                in_flight -= 1
            return result

        file_listener._process_current_folder = instrumented

        await file_listener._process_folder_recursively(temp_watch_folder)

        assert max_in_flight > 1  # actually concurrent, not accidentally serialized
        assert max_in_flight <= 3  # respects folder_semaphore

    async def test_all_folders_still_processed_correctly(self, file_listener, temp_watch_folder):
        """Concurrent traversal still visits and uploads every folder in the tree (no folder dropped)."""
        for i in range(5):
            d = temp_watch_folder / f"event_{i}"
            d.mkdir()
            (d / "photo.jpg").write_text(f"content {i}")

        await file_listener._process_folder_recursively(temp_watch_folder)

        for i in range(5):
            info_file = temp_watch_folder / f"event_{i}" / ".milo_backup.info"
            assert info_file.exists()
            data = json.loads(info_file.read_text())
            assert "photo.jpg" in data["files"]

        stats = file_listener.get_statistics()
        # 5 new event folders + root + existing subdir/nested from the fixture
        assert stats["scanned_folders"] >= 5


class TestFileListenerUploads:
    """Test FileListener upload operations."""

    async def test_upload_files_success(self, file_listener, temp_watch_folder):
        """Test successful file upload — _upload_files now returns Dict[str, float]."""
        files_to_upload = ["file1.txt", "file2.txt"]

        upload_mtimes = await file_listener._upload_files(files_to_upload, temp_watch_folder)

        # Concurrent gather returns a dict of successfully uploaded filename -> upload_mtime
        assert len(upload_mtimes) == 2
        assert set(upload_mtimes.keys()) == {"file1.txt", "file2.txt"}
        # Each value is a float mtime
        for mtime in upload_mtimes.values():
            assert isinstance(mtime, float)

        # Verify S3Manager was called
        assert file_listener.s3_manager.upload_file.call_count == 2

    async def test_upload_with_timeout_scales_with_file_size(self, file_listener, temp_watch_folder):
        """PERF-05: the per-file timeout window is computed from that file's actual size."""
        with patch(
            "aws_copier.core.file_listener.estimate_upload_timeout", wraps=estimate_upload_timeout
        ) as spy:
            await file_listener._upload_with_timeout("file1.txt", temp_watch_folder)

        expected_size = (temp_watch_folder / "file1.txt").stat().st_size
        spy.assert_called_once_with(expected_size)

    async def test_upload_with_timeout_missing_file_uses_floor(self, file_listener, temp_watch_folder):
        """A file that can't be stat'd (e.g. deleted mid-scan) falls back to size 0 (floor timeout)."""
        with patch(
            "aws_copier.core.file_listener.estimate_upload_timeout", wraps=estimate_upload_timeout
        ) as spy:
            await file_listener._upload_with_timeout("does_not_exist.txt", temp_watch_folder)

        spy.assert_called_once_with(0)

    async def test_semaphore_wait_does_not_count_against_timeout(self, file_listener, temp_watch_folder):
        """CONC-01 regression: a file queued behind a slow upload must not time out merely from waiting.

        Before the fix, the timeout clock started at task creation — including time spent
        blocked on the semaphore. With many small files queued behind a few slow/large
        uploads all sharing one semaphore slot, queued files would time out having done
        nothing at all, the instant their window elapsed while still waiting in line.
        """
        file_listener.upload_semaphore = asyncio.Semaphore(1)

        slow_file = temp_watch_folder / "slow_occupant.bin"
        slow_file.write_bytes(b"S" * 100)
        queued_file = temp_watch_folder / "queued_fast.bin"
        queued_file.write_bytes(b"Q" * 50)

        async def upload_side_effect(local_path, s3_key, precomputed_md5=None):
            if local_path.name == "slow_occupant.bin":
                await asyncio.sleep(0.2)
            return True

        file_listener.s3_manager.check_exists.return_value = False
        file_listener.s3_manager.upload_file.side_effect = upload_side_effect

        with patch(
            "aws_copier.core.file_listener.estimate_upload_timeout",
            side_effect=lambda size: 5.0 if size == 100 else 0.05,
        ):
            # Start the slow upload first and yield once so it acquires the sole semaphore
            # slot before the queued file's task is even created — deterministic ordering.
            slow_task = asyncio.create_task(file_listener._upload_with_timeout("slow_occupant.bin", temp_watch_folder))
            await asyncio.sleep(0)
            queued_task = asyncio.create_task(
                file_listener._upload_with_timeout("queued_fast.bin", temp_watch_folder)
            )
            slow_result, queued_result = await asyncio.gather(slow_task, queued_task)

        assert slow_result[1] is True
        # The queued file's own 0.05s window only starts once it acquires the semaphore
        # (after the ~0.2s slow upload releases it) — its actual upload is near-instant, so
        # it must succeed rather than time out from the ~0.2s it spent merely queued.
        assert queued_result[1] is True

    async def test_upload_files_with_s3_check_skip(self, file_listener, temp_watch_folder):
        """Test upload with S3 existence check - files already exist in S3."""
        # Configure mock to return True for check_exists (file already exists)
        file_listener.s3_manager.check_exists.return_value = True

        files_to_upload = ["file1.txt"]

        upload_mtimes = await file_listener._upload_files(files_to_upload, temp_watch_folder)

        # When check_exists returns True, _upload_single_file returns (True, mtime), so filename is in dict
        assert len(upload_mtimes) == 1
        assert "file1.txt" in upload_mtimes
        file_listener.s3_manager.upload_file.assert_not_called()

    async def test_upload_single_file_success(self, file_listener, temp_watch_folder):
        """Test uploading a single file — returns (True, mtime) tuple."""
        filename = "file1.txt"

        ok, upload_mtime = await file_listener._upload_single_file(filename, temp_watch_folder)

        assert ok is True
        assert isinstance(upload_mtime, float)
        assert upload_mtime > 0.0
        file_listener.s3_manager.upload_file.assert_called_once()

    async def test_upload_single_file_already_exists(self, file_listener, temp_watch_folder):
        """Test uploading a file that already exists in S3 — returns (True, mtime) tuple."""
        # Configure mock to return True for check_exists
        file_listener.s3_manager.check_exists.return_value = True

        filename = "file1.txt"

        ok, upload_mtime = await file_listener._upload_single_file(filename, temp_watch_folder)

        assert ok is True
        assert isinstance(upload_mtime, float)
        file_listener.s3_manager.upload_file.assert_not_called()

    async def test_concurrent_upload_with_semaphore(self, file_listener, temp_watch_folder):
        """Test that concurrent uploads respect semaphore limit."""
        # Create many files to test semaphore
        files_to_upload = [f"file_{i}.txt" for i in range(10)]

        # Create the actual files
        for filename in files_to_upload:
            (temp_watch_folder / filename).write_text(f"Content of {filename}")

        # Upload files
        upload_mtimes = await file_listener._upload_files(files_to_upload, temp_watch_folder)

        # All files should be uploaded
        assert len(upload_mtimes) == 10

        # Verify semaphore was used (check that upload_file was called for each file)
        assert file_listener.s3_manager.upload_file.call_count == 10

    async def test_partial_upload_backup_info_update(self, file_listener, temp_watch_folder):
        """Test that backup info only includes successfully uploaded files."""
        # Create test files
        test_files = ["success1.txt", "success2.txt", "fail1.txt", "fail2.txt"]
        for filename in test_files:
            (temp_watch_folder / filename).write_text(f"content of {filename}")

        # Configure mock to simulate partial success
        def mock_upload_side_effect(file_path, s3_key, **kwargs):
            filename = file_path.name
            return filename.startswith("success")  # Only "success*" files succeed

        file_listener.s3_manager.upload_file.side_effect = mock_upload_side_effect
        file_listener.s3_manager.check_exists.return_value = False

        # Process the folder
        await file_listener._process_current_folder(temp_watch_folder)

        # Check backup info file
        backup_info_file = temp_watch_folder / ".milo_backup.info"
        assert backup_info_file.exists()

        import json

        with open(backup_info_file, "r", encoding="utf-8") as f:
            backup_info = json.load(f)

        backed_up_files = backup_info["files"]

        # Only successful uploads should be in backup info
        assert "success1.txt" in backed_up_files
        assert "success2.txt" in backed_up_files
        assert "fail1.txt" not in backed_up_files
        assert "fail2.txt" not in backed_up_files

        # Test retry behavior - process again
        file_listener.s3_manager.upload_file.reset_mock()
        await file_listener._process_current_folder(temp_watch_folder)

        # Should only attempt to upload the failed files
        upload_calls = [call.args[0].name for call in file_listener.s3_manager.upload_file.call_args_list]
        assert "success1.txt" not in upload_calls  # Already successful
        assert "success2.txt" not in upload_calls  # Already successful
        assert "fail1.txt" in upload_calls  # Should retry
        assert "fail2.txt" in upload_calls  # Should retry


class TestFileListenerUtilities:
    """Test FileListener utility functions."""

    async def test_build_s3_key(self, file_listener, temp_watch_folder):
        """Test building S3 keys for files."""
        file_path = temp_watch_folder / "subdir" / "file3.txt"

        s3_key = file_listener._build_s3_key(file_path)

        # Should use watch folder name as prefix
        expected_key = f"{temp_watch_folder.name}/subdir/file3.txt"
        assert s3_key == expected_key

    async def test_calculate_md5(self, file_listener, temp_watch_folder):
        """Test MD5 calculation."""
        file_path = temp_watch_folder / "file1.txt"

        md5_hash = await file_listener._calculate_md5(file_path)

        # Calculate expected MD5
        import hashlib

        expected_md5 = hashlib.md5("Content of file 1".encode()).hexdigest()

        assert md5_hash == expected_md5

    async def test_calculate_md5_nonexistent_file(self, file_listener):
        """Test MD5 calculation for non-existent file."""
        file_path = Path("/nonexistent/file.txt")

        md5_hash = await file_listener._calculate_md5(file_path)

        assert md5_hash is None


class TestFileListenerBackupInfo:
    """Test FileListener backup info management."""

    async def test_load_backup_info_existing_file(self, file_listener, temp_watch_folder):
        """Test loading existing backup info file — old string format is migrated to {md5, mtime}."""
        # Create a backup info file with the old string format
        backup_info_file = temp_watch_folder / ".milo_backup.info"
        test_data = {
            "timestamp": "2023-01-01T00:00:00",
            "files": {"file1.txt": "md5_hash_1", "file2.txt": "md5_hash_2"},
        }

        with open(backup_info_file, "w") as f:
            json.dump(test_data, f)

        # Load backup info — old string entries are migrated to new {md5, mtime} dict format (D-01)
        loaded_info = await file_listener._load_backup_info(backup_info_file)

        assert loaded_info == {
            "file1.txt": {"md5": "md5_hash_1", "mtime": 0.0, "s3_key": None},
            "file2.txt": {"md5": "md5_hash_2", "mtime": 0.0, "s3_key": None},
        }

    async def test_load_backup_info_nonexistent_file(self, file_listener, temp_watch_folder):
        """Test loading non-existent backup info file."""
        backup_info_file = temp_watch_folder / "nonexistent.milo_backup.info"

        loaded_info = await file_listener._load_backup_info(backup_info_file)

        assert loaded_info == {}

    async def test_update_backup_info(self, file_listener, temp_watch_folder):
        """Test updating backup info file with new {md5, mtime} dict format (D-03)."""
        backup_info_file = temp_watch_folder / ".milo_backup.info"
        current_files = {
            "file1.txt": {"md5": "md5_hash_1", "mtime": 1.0},
            "file2.txt": {"md5": "md5_hash_2", "mtime": 2.0},
        }

        await file_listener._update_backup_info(backup_info_file, current_files)

        # Verify file was created
        assert backup_info_file.exists()

        # Verify content
        with open(backup_info_file) as f:
            data = json.load(f)

        assert "timestamp" in data
        assert data["files"] == current_files


class TestFileListenerStatistics:
    """Test FileListener statistics tracking."""

    async def test_statistics_tracking(self, file_listener, temp_watch_folder):
        """Test that statistics are properly tracked."""
        # Reset statistics
        file_listener.reset_statistics()

        # Run scan
        await file_listener.scan_all_folders()

        stats = file_listener.get_statistics()

        assert stats["scanned_folders"] > 0
        assert stats["scanned_files"] > 0
        assert stats["uploaded_files"] >= 0


class TestFileListenerS3KeyBuilding:
    """Test S3 key building functionality with folder mapping."""

    def test_build_s3_key_with_list_format(self, mock_s3_manager):
        """Test S3 key building with legacy list format."""
        config = SimpleConfig(watch_folders=["/Users/test/Documents", "/Users/test/Pictures"])
        file_listener = FileListener(config, mock_s3_manager)

        # Test file in Documents folder
        docs_file = Path("/Users/test/Documents/subfolder/test.txt")
        s3_key = file_listener._build_s3_key(docs_file)
        assert s3_key == "Documents/subfolder/test.txt"

        # Test file in Pictures folder
        pics_file = Path("/Users/test/Pictures/vacation/photo.jpg")
        s3_key = file_listener._build_s3_key(pics_file)
        assert s3_key == "Pictures/vacation/photo.jpg"

    def test_build_s3_key_with_dict_format(self, mock_s3_manager):
        """Test S3 key building with new dictionary format."""
        config = SimpleConfig(
            watch_folders={"/Users/test/Documents": "MyDocuments", "E:/": "Pictures", "/home/user/videos": "Videos"}
        )
        file_listener = FileListener(config, mock_s3_manager)

        # Test file in Documents folder with custom S3 name
        docs_file = Path("/Users/test/Documents/work/report.pdf")
        s3_key = file_listener._build_s3_key(docs_file)
        assert s3_key == "MyDocuments/work/report.pdf"

        # Test file in E:/ drive mapped to Pictures
        e_drive_file = Path("E:/photos/vacation/image.jpg")
        s3_key = file_listener._build_s3_key(e_drive_file)
        assert s3_key == "Pictures/photos/vacation/image.jpg"

        # Test file in videos folder
        video_file = Path("/home/user/videos/movie.mp4")
        s3_key = file_listener._build_s3_key(video_file)
        assert s3_key == "Videos/movie.mp4"

    def test_build_s3_key_with_windows_paths(self, mock_s3_manager):
        """Test S3 key building with Windows-style paths."""
        # Use normalized paths that work cross-platform
        docs_path = str(Path("C:/Users/test/Documents"))
        photos_path = str(Path("D:/Photos"))

        config = SimpleConfig(watch_folders={docs_path: "Documents", photos_path: "Pictures"})
        file_listener = FileListener(config, mock_s3_manager)

        # Test file in Documents folder
        win_file = Path(docs_path) / "folder" / "file.txt"
        s3_key = file_listener._build_s3_key(win_file)
        # Should use forward slashes in S3 key and map to custom name
        assert s3_key == "Documents/folder/file.txt"
        assert "\\" not in s3_key

    def test_build_s3_key_file_not_in_watch_folders(self, mock_s3_manager):
        """Test S3 key building for file not in any watch folder."""
        config = SimpleConfig(watch_folders={"/Users/test/Documents": "Documents"})
        file_listener = FileListener(config, mock_s3_manager)

        # File outside watch folders should use absolute path as fallback
        outside_file = Path("/tmp/random/file.txt")
        s3_key = file_listener._build_s3_key(outside_file)
        assert s3_key == "/tmp/random/file.txt"

    def test_build_s3_key_nested_folders(self, mock_s3_manager):
        """Test S3 key building with deeply nested folder structures."""
        config = SimpleConfig(watch_folders={"/Users/test/Projects": "Development"})
        file_listener = FileListener(config, mock_s3_manager)

        # Test deeply nested file
        nested_file = Path("/Users/test/Projects/python/aws-copier/src/main.py")
        s3_key = file_listener._build_s3_key(nested_file)
        assert s3_key == "Development/python/aws-copier/src/main.py"

    def test_build_s3_key_root_folder_file(self, mock_s3_manager):
        """Test S3 key building for file directly in watch folder root."""
        config = SimpleConfig(watch_folders={"/Users/test/Documents": "MyDocs"})
        file_listener = FileListener(config, mock_s3_manager)

        # File directly in watch folder root
        root_file = Path("/Users/test/Documents/readme.txt")
        s3_key = file_listener._build_s3_key(root_file)
        assert s3_key == "MyDocs/readme.txt"


class TestFileListenerConfig:
    """CONFIG-01: upload_semaphore respects config.max_concurrent_uploads."""

    async def test_config_max_concurrent_uploads_wires_to_semaphore(self, tmp_path):
        """Verify upload_semaphore._value equals the configured max_concurrent_uploads."""
        config = SimpleConfig(
            aws_access_key_id="x",
            aws_secret_access_key="y",
            aws_region="us-east-1",
            s3_bucket="b",
            s3_prefix="",
            watch_folders=[str(tmp_path)],
            max_concurrent_uploads=7,
        )
        fl = FileListener(config, AsyncMock())
        assert fl.upload_semaphore._value == 7


class TestFileListenerAsyncBackupIO:
    """ASYNC-03: backup info I/O uses aiofiles under a per-folder asyncio.Lock."""

    async def test_load_backup_info_uses_aiofiles_and_lock(self, file_listener, tmp_path):
        """Confirm _load_backup_info calls aiofiles.open and returns correct data (migrated to new format)."""
        from unittest.mock import patch

        backup_file = tmp_path / ".milo_backup.info"
        # Write old string format — should be migrated to {md5, mtime} dict on load (D-01)
        backup_file.write_text('{"timestamp": "2026-01-01T00:00:00", "files": {"a.txt": "hash1"}}')
        with patch("aws_copier.core.file_listener.aiofiles.open", wraps=__import__("aiofiles").open) as mock_open:
            result = await file_listener._load_backup_info(backup_file)
        assert result == {"a.txt": {"md5": "hash1", "mtime": 0.0, "s3_key": None}}
        assert mock_open.call_count == 1

    async def test_update_backup_info_uses_aiofiles_and_lock(self, file_listener, tmp_path):
        """Confirm _update_backup_info calls aiofiles.open and writes correct data."""
        from unittest.mock import patch

        backup_file = tmp_path / ".milo_backup.info"
        new_format_files = {"a.txt": {"md5": "hash1", "mtime": 1.0}}
        with patch("aws_copier.core.file_listener.aiofiles.open", wraps=__import__("aiofiles").open) as mock_open:
            await file_listener._update_backup_info(backup_file, new_format_files)
        assert backup_file.exists()
        assert mock_open.call_count == 1
        import json

        data = json.loads(backup_file.read_text())
        assert data["files"] == new_format_files

    async def test_folder_lock_is_same_instance_per_folder(self, file_listener, tmp_path):
        """_get_folder_lock returns the same Lock instance for the same folder path."""
        lock_a = file_listener._get_folder_lock(tmp_path)
        lock_b = file_listener._get_folder_lock(tmp_path)
        assert lock_a is lock_b
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        lock_c = file_listener._get_folder_lock(other_dir)
        assert lock_a is not lock_c


class TestFileListenerConcurrentUpload:
    """ASYNC-02: _upload_files runs uploads concurrently via asyncio.gather, not serially."""

    async def test_upload_files_runs_concurrently(self, file_listener, temp_watch_folder):
        """10 files with 0.2s artificial delay each must finish in well under 2s (serial) — around 0.3s."""
        import time

        async def slow_upload(file_path, s3_key, **kwargs):
            await asyncio.sleep(0.2)
            return True

        file_listener.s3_manager.check_exists = AsyncMock(return_value=False)
        file_listener.s3_manager.upload_file = AsyncMock(side_effect=slow_upload)

        files = [f"f{i}.txt" for i in range(10)]
        for name in files:
            (temp_watch_folder / name).write_text("x")

        start = time.monotonic()
        uploaded = await file_listener._upload_files(files, temp_watch_folder)
        elapsed = time.monotonic() - start

        assert len(uploaded) == 10
        # Serial would be >= 2.0s. Concurrent with semaphore >=10 must be under 1.0s.
        assert elapsed < 1.0, f"Uploads ran serially (elapsed={elapsed:.2f}s)"

    async def test_upload_files_active_tasks_tracked(self, file_listener, temp_watch_folder):
        """_active_upload_tasks is populated during gather and emptied by done_callback after."""
        file_listener.s3_manager.check_exists = AsyncMock(return_value=False)
        file_listener.s3_manager.upload_file = AsyncMock(return_value=True)

        files = [f"t{i}.txt" for i in range(3)]
        for name in files:
            (temp_watch_folder / name).write_text("x")

        assert len(file_listener._active_upload_tasks) == 0
        await file_listener._upload_files(files, temp_watch_folder)
        # After gather completes, the done_callback has discarded every task.
        assert len(file_listener._active_upload_tasks) == 0

    async def test_upload_files_gather_handles_exceptions(self, file_listener, temp_watch_folder):
        """One raising coroutine must not cancel the rest; return_exceptions=True preserves partial success."""
        calls = {"n": 0}

        async def maybe_fail(file_path, s3_key, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated failure")
            return True

        file_listener.s3_manager.check_exists = AsyncMock(return_value=False)
        file_listener.s3_manager.upload_file = AsyncMock(side_effect=maybe_fail)

        files = ["a.txt", "b.txt", "c.txt"]
        for name in files:
            (temp_watch_folder / name).write_text("x")

        uploaded = await file_listener._upload_files(files, temp_watch_folder)
        # Exactly one failed; two succeeded.
        assert len(uploaded) == 2


class TestFileListenerIgnoreIntegration:
    """IGNORE-03: FileListener delegates to IGNORE_RULES. IGNORE-04: ignored files increment stats."""

    async def test_file_listener_has_no_local_ignore_attrs(self, file_listener):
        """IGNORE-03: old per-instance ignore sets and methods must be absent."""
        assert not hasattr(file_listener, "ignore_patterns")
        assert not hasattr(file_listener, "ignore_dirs")
        assert not hasattr(file_listener, "_should_ignore_file")
        assert not hasattr(file_listener, "_should_ignore_directory")

    async def test_scan_increments_ignored_files_stat(self, file_listener, temp_watch_folder):
        """IGNORE-04: _stats['ignored_files'] increments for each file blocked by IGNORE_RULES."""
        # Drop a sensitive file that IGNORE_RULES will block (.env starts with dot)
        (temp_watch_folder / ".env").write_text("SECRET=x")
        (temp_watch_folder / "regular.txt").write_text("ok")

        before = file_listener._stats["ignored_files"]
        await file_listener._scan_current_files(temp_watch_folder)
        after = file_listener._stats["ignored_files"]

        assert after - before >= 1  # at least the .env file


class TestBackupInfoMigrationAndCache:
    """PERF-01 / PERF-02: backup info format migration and in-memory cache."""

    async def test_load_migrates_old_string_format(self, file_listener, temp_watch_folder):
        """Old string-format entries are migrated to {md5, mtime} dict on read (D-01)."""
        import json

        backup_file = temp_watch_folder / ".milo_backup.info"
        backup_file.write_text(json.dumps({"files": {"a.txt": "abc123"}}))
        result = await file_listener._load_backup_info(backup_file)
        assert result == {"a.txt": {"md5": "abc123", "mtime": 0.0, "s3_key": None}}

    async def test_load_preserves_new_dict_format(self, file_listener, temp_watch_folder):
        """Dict-format entries that already include s3_key pass through unchanged."""
        import json

        backup_file = temp_watch_folder / ".milo_backup.info"
        payload = {"files": {"a.txt": {"md5": "abc123", "mtime": 1234.5, "s3_key": "Documents/a.txt"}}}
        backup_file.write_text(json.dumps(payload))
        result = await file_listener._load_backup_info(backup_file)
        assert result == {"a.txt": {"md5": "abc123", "mtime": 1234.5, "s3_key": "Documents/a.txt"}}

    async def test_load_backfills_s3_key_when_missing(self, file_listener, temp_watch_folder):
        """Dict-format entries from before the s3_key field existed are backfilled with None (MOVE-01)."""
        import json

        backup_file = temp_watch_folder / ".milo_backup.info"
        payload = {"files": {"a.txt": {"md5": "abc123", "mtime": 1234.5}}}
        backup_file.write_text(json.dumps(payload))
        result = await file_listener._load_backup_info(backup_file)
        assert result == {"a.txt": {"md5": "abc123", "mtime": 1234.5, "s3_key": None}}

    async def test_load_returns_empty_when_file_missing(self, file_listener, temp_watch_folder):
        """Non-existent backup info file returns empty dict."""
        backup_file = temp_watch_folder / "does_not_exist.info"
        result = await file_listener._load_backup_info(backup_file)
        assert result == {}

    async def test_load_uses_cache_when_disk_mtime_unchanged(self, file_listener, temp_watch_folder):
        """Second call with same disk mtime hits the in-memory cache without re-reading disk (PERF-02)."""
        import json

        backup_file = temp_watch_folder / ".milo_backup.info"
        backup_file.write_text(json.dumps({"files": {"a.txt": {"md5": "x", "mtime": 1.0}}}))
        # First call populates cache
        await file_listener._load_backup_info(backup_file)
        # Spy on aiofiles.open via patch — second call must NOT read disk
        import aws_copier.core.file_listener as flmod
        from unittest.mock import patch

        with patch.object(flmod, "aiofiles") as mock_af:
            result = await file_listener._load_backup_info(backup_file)
            mock_af.open.assert_not_called()
        assert result == {"a.txt": {"md5": "x", "mtime": 1.0, "s3_key": None}}

    async def test_load_re_reads_when_disk_mtime_changes(self, file_listener, temp_watch_folder):
        """Disk read is triggered when .milo_backup.info mtime changes (cache miss)."""
        import json
        import time

        backup_file = temp_watch_folder / ".milo_backup.info"
        backup_file.write_text(json.dumps({"files": {"a.txt": {"md5": "old", "mtime": 1.0}}}))
        first = await file_listener._load_backup_info(backup_file)
        assert first["a.txt"]["md5"] == "old"
        # Force a new mtime
        time.sleep(0.05)
        backup_file.write_text(json.dumps({"files": {"a.txt": {"md5": "new", "mtime": 2.0}}}))
        second = await file_listener._load_backup_info(backup_file)
        assert second["a.txt"]["md5"] == "new"

    async def test_update_writes_dict_format(self, file_listener, temp_watch_folder):
        """_update_backup_info writes {md5, mtime} dict values to disk (D-03)."""
        import json

        backup_file = temp_watch_folder / ".milo_backup.info"
        payload = {"a.txt": {"md5": "x", "mtime": 1.0}}
        ok = await file_listener._update_backup_info(backup_file, payload)
        assert ok is True
        raw = json.loads(backup_file.read_text())
        assert raw["files"]["a.txt"] == {"md5": "x", "mtime": 1.0}

    async def test_update_invalidates_cache(self, file_listener, temp_watch_folder):
        """After _update_backup_info, subsequent _load_backup_info reflects the new content."""
        import json

        backup_file = temp_watch_folder / ".milo_backup.info"
        backup_file.write_text(json.dumps({"files": {"a.txt": {"md5": "v1", "mtime": 1.0}}}))
        # Prime cache
        first = await file_listener._load_backup_info(backup_file)
        assert first["a.txt"]["md5"] == "v1"
        # Update through the API
        await file_listener._update_backup_info(backup_file, {"a.txt": {"md5": "v2", "mtime": 2.0}})
        # Subsequent load must reflect the update
        second = await file_listener._load_backup_info(backup_file)
        assert second["a.txt"]["md5"] == "v2"

    async def test_clear_caches_empties_cache_and_mtime_dicts(self, file_listener, temp_watch_folder):
        """MEM-01: clear_caches() drops the PERF-02 cache so long-running memory doesn't grow unbounded."""
        import json

        backup_file = temp_watch_folder / ".milo_backup.info"
        backup_file.write_text(json.dumps({"files": {"a.txt": {"md5": "x", "mtime": 1.0}}}))
        await file_listener._load_backup_info(backup_file)  # populates the cache
        assert file_listener._backup_info_cache
        assert file_listener._backup_info_mtime

        file_listener.clear_caches()

        assert file_listener._backup_info_cache == {}
        assert file_listener._backup_info_mtime == {}

    async def test_clear_caches_forces_disk_reread_but_preserves_content(self, file_listener, temp_watch_folder):
        """After clearing, the next load is a cache miss but still returns correct data (no data loss)."""
        import json

        backup_file = temp_watch_folder / ".milo_backup.info"
        backup_file.write_text(json.dumps({"files": {"a.txt": {"md5": "x", "mtime": 1.0}}}))
        await file_listener._load_backup_info(backup_file)
        file_listener.clear_caches()

        import aws_copier.core.file_listener as flmod
        from unittest.mock import patch

        with patch.object(flmod, "aiofiles", wraps=flmod.aiofiles) as spy_aiofiles:
            result = await file_listener._load_backup_info(backup_file)
            spy_aiofiles.open.assert_called_once()
        assert result == {"a.txt": {"md5": "x", "mtime": 1.0, "s3_key": None}}

    def test_clear_caches_does_not_remove_folder_locks(self, file_listener, temp_watch_folder):
        """_folder_locks is intentionally untouched — small, and unsafe to prune mid-flight."""
        lock = file_listener._get_folder_lock(temp_watch_folder)
        file_listener.clear_caches()
        assert file_listener._get_folder_lock(temp_watch_folder) is lock


class TestMtimeSkip:
    """PERF-01: mtime-skip in _scan_current_files and D-02 upload_mtime capture."""

    async def test_unchanged_file_skips_md5(self, file_listener, temp_watch_folder):
        """Second scan cycle with unchanged file increments skipped_files, does not call _calculate_md5."""
        from unittest.mock import patch

        # First full cycle — uploads file1.txt and establishes backup state
        await file_listener._process_current_folder(temp_watch_folder)
        file_listener.reset_statistics()

        # Second cycle: file1.txt unchanged — mtime-skip must fire
        with patch.object(
            file_listener, "_calculate_md5", wraps=file_listener._calculate_md5
        ) as spy_md5:
            await file_listener._process_current_folder(temp_watch_folder)
            # No MD5 should be computed for the unchanged files
            spy_md5.assert_not_called()

        stats = file_listener.get_statistics()
        assert stats["skipped_files"] >= 1

    async def test_mtime_change_triggers_upload(self, file_listener, temp_watch_folder):
        """Modifying a file's content (advancing mtime) causes _calculate_md5 and upload_file to be called."""
        import time
        from unittest.mock import patch

        # First cycle
        await file_listener._process_current_folder(temp_watch_folder)
        file_listener.s3_manager.upload_file.reset_mock()
        file_listener.reset_statistics()

        # Modify file1.txt to advance its mtime
        time.sleep(0.05)
        (temp_watch_folder / "file1.txt").write_text("Modified content")

        with patch.object(
            file_listener, "_calculate_md5", wraps=file_listener._calculate_md5
        ) as spy_md5:
            await file_listener._process_current_folder(temp_watch_folder)
            # MD5 must be computed for the modified file
            assert spy_md5.call_count >= 1

        file_listener.s3_manager.upload_file.assert_called()
        upload_paths = [c.args[0].name for c in file_listener.s3_manager.upload_file.call_args_list]
        assert "file1.txt" in upload_paths

    async def test_first_run_after_migration_recomputes(self, file_listener, temp_watch_folder):
        """A migrated old-format entry (mtime=0.0) does not match real st_mtime, so MD5 is recomputed."""
        import json
        import hashlib
        from unittest.mock import patch

        # Pre-create .milo_backup.info with old string format
        backup_file = temp_watch_folder / ".milo_backup.info"
        # Use the correct md5 for file1.txt so the S3 check would skip upload if mtime matched —
        # but because mtime=0.0 != real st_mtime the file must be re-processed regardless.
        real_md5 = hashlib.md5(b"Content of file 1").hexdigest()
        backup_file.write_text(json.dumps({"files": {"file1.txt": real_md5}}))

        with patch.object(
            file_listener, "_calculate_md5", wraps=file_listener._calculate_md5
        ) as spy_md5:
            await file_listener._process_current_folder(temp_watch_folder)
            # _calculate_md5 must have been called for file1.txt (mtime=0.0 forces re-stat)
            assert spy_md5.call_count >= 1

        # Post-write .milo_backup.info must be in new dict format
        raw = json.loads(backup_file.read_text())
        for entry in raw["files"].values():
            assert isinstance(entry, dict)
            assert "md5" in entry
            assert "mtime" in entry

    async def test_stored_mtime_is_pre_upload_capture(self, file_listener, temp_watch_folder):
        """The mtime stored in backup info after upload equals the value captured just before upload (D-02)."""
        import json
        from unittest.mock import AsyncMock

        # Capture the pre-upload mtime from within the upload coroutine
        captured_mtimes: dict = {}

        async def upload_and_record(file_path, s3_key, **kwargs):
            # Record the mtime at the moment upload is called
            captured_mtimes[file_path.name] = file_path.stat().st_mtime
            return True

        file_listener.s3_manager.upload_file = AsyncMock(side_effect=upload_and_record)
        file_listener.s3_manager.check_exists = AsyncMock(return_value=False)

        await file_listener._process_current_folder(temp_watch_folder)

        # Read back the stored backup info
        backup_file = temp_watch_folder / ".milo_backup.info"
        raw = json.loads(backup_file.read_text())

        for filename, stored_entry in raw["files"].items():
            if filename in captured_mtimes:
                assert isinstance(stored_entry, dict), f"{filename} not in new format"
                # The stored mtime must be <= the mtime captured at upload call time
                # (captured just before upload, not post-modification)
                assert stored_entry["mtime"] <= captured_mtimes[filename], (
                    f"{filename}: stored mtime {stored_entry['mtime']} > captured {captured_mtimes[filename]}"
                )


class TestBackupignoreCascade:
    """CONFIG-06: per-directory .backupignore with ancestor cascade (D-07, D-08)."""

    async def test_root_backupignore_excludes_match(self, file_listener, temp_watch_folder, mock_s3_manager):
        """Root .backupignore with '*.tmp' excludes matching files from upload."""
        (temp_watch_folder / ".backupignore").write_text("*.tmp\n")
        (temp_watch_folder / "keep.txt").write_text("k")
        (temp_watch_folder / "drop.tmp").write_text("d")
        await file_listener._process_current_folder(temp_watch_folder)
        uploaded = [c.args[0].name for c in mock_s3_manager.upload_file.call_args_list]
        assert "keep.txt" in uploaded
        assert "drop.tmp" not in uploaded

    async def test_root_backupignore_cascades_to_subdir(self, file_listener, temp_watch_folder, mock_s3_manager):
        """D-07: root .backupignore patterns apply to all subdirectories."""
        (temp_watch_folder / ".backupignore").write_text("*.tmp\n")
        sub = temp_watch_folder / "photos"
        sub.mkdir()
        (sub / "keep.txt").write_text("k")
        (sub / "drop.tmp").write_text("d")
        await file_listener._process_current_folder(sub)
        uploaded = [c.args[0].name for c in mock_s3_manager.upload_file.call_args_list]
        assert "keep.txt" in uploaded
        assert "drop.tmp" not in uploaded

    async def test_child_backupignore_adds_to_root_rules(self, file_listener, temp_watch_folder, mock_s3_manager):
        """D-08: child .backupignore adds to (not replaces) ancestor rules."""
        (temp_watch_folder / ".backupignore").write_text("*.tmp\n")
        sub = temp_watch_folder / "docs"
        sub.mkdir()
        (sub / ".backupignore").write_text("*.log\n")
        (sub / "x.tmp").write_text("a")
        (sub / "y.log").write_text("b")
        (sub / "z.txt").write_text("c")
        await file_listener._process_current_folder(sub)
        uploaded = [c.args[0].name for c in mock_s3_manager.upload_file.call_args_list]
        assert "z.txt" in uploaded
        assert "x.tmp" not in uploaded
        assert "y.log" not in uploaded

    async def test_no_backupignore_no_filtering(self, file_listener, temp_watch_folder, mock_s3_manager):
        """No .backupignore file means no filtering; all normal files are uploaded."""
        (temp_watch_folder / "a.txt").write_text("a")
        (temp_watch_folder / "b.txt").write_text("b")
        await file_listener._process_current_folder(temp_watch_folder)
        uploaded = sorted(c.args[0].name for c in mock_s3_manager.upload_file.call_args_list)
        assert "a.txt" in uploaded
        assert "b.txt" in uploaded

    async def test_unreadable_backupignore_does_not_crash(self, file_listener, temp_watch_folder, caplog):
        """Unreadable .backupignore logs a warning and returns a no-op PathSpec."""
        import logging

        ignore_file = temp_watch_folder / ".backupignore"
        ignore_file.write_bytes(b"\xff\xfe\x00\x00not-utf8")  # binary; utf-8 decode fails
        with caplog.at_level(logging.WARNING):
            spec = file_listener._load_backupignore_spec(temp_watch_folder, temp_watch_folder)
        assert any("Could not read" in r.message for r in caplog.records)
        # Spec should be a valid no-op PathSpec
        assert not spec.match_file("anything.tmp")

    async def test_directory_scoped_pattern_matches_via_relative_path(
        self, file_listener, temp_watch_folder, mock_s3_manager
    ):
        """Pitfall 4: directory-scoped patterns (raw/*.jpg) work when match_file uses relative paths with forward slashes."""
        (temp_watch_folder / ".backupignore").write_text("raw/*.jpg\n")
        raw = temp_watch_folder / "raw"
        raw.mkdir()
        (raw / "shot.jpg").write_text("img")
        (raw / "notes.txt").write_text("n")
        await file_listener._process_current_folder(raw)
        uploaded = [c.args[0].name for c in mock_s3_manager.upload_file.call_args_list]
        assert "notes.txt" in uploaded
        assert "shot.jpg" not in uploaded
