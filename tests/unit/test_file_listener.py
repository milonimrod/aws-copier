"""
Comprehensive tests for FileListener with proper S3Manager mocking.
Tests the incremental backup functionality without testing S3 operations.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aws_copier.core.file_listener import FileListener
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

        assert root_data["files"]["file1.txt"] == expected_md5

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
        """Test scanning files in a folder and computing MD5s."""
        folder_path = temp_watch_folder

        current_files = await file_listener._scan_current_files(folder_path)

        assert "file1.txt" in current_files
        assert "file2.txt" in current_files
        assert len(current_files) == 2  # Should not include subdirectories

        # Verify MD5 calculation
        import hashlib

        expected_md5 = hashlib.md5("Content of file 1".encode()).hexdigest()
        assert current_files["file1.txt"] == expected_md5

    async def test_determine_files_to_upload_new_files(self, file_listener):
        """Test determining which files need upload when all are new."""
        current_files = {"file1.txt": "md5_hash_1", "file2.txt": "md5_hash_2"}
        existing_backup_info = {}

        files_to_upload = file_listener._determine_files_to_upload(current_files, existing_backup_info)

        assert set(files_to_upload) == {"file1.txt", "file2.txt"}

    async def test_determine_files_to_upload_changed_files(self, file_listener):
        """Test determining which files need upload when some have changed."""
        current_files = {"file1.txt": "new_md5_hash_1", "file2.txt": "md5_hash_2"}
        existing_backup_info = {"file1.txt": "old_md5_hash_1", "file2.txt": "md5_hash_2"}

        files_to_upload = file_listener._determine_files_to_upload(current_files, existing_backup_info)

        assert files_to_upload == ["file1.txt"]  # Only changed file

    async def test_determine_files_to_upload_no_changes(self, file_listener):
        """Test determining which files need upload when nothing changed."""
        current_files = {"file1.txt": "md5_hash_1", "file2.txt": "md5_hash_2"}
        existing_backup_info = {"file1.txt": "md5_hash_1", "file2.txt": "md5_hash_2"}

        files_to_upload = file_listener._determine_files_to_upload(current_files, existing_backup_info)

        assert files_to_upload == []


class TestFileListenerUploads:
    """Test FileListener upload operations."""

    async def test_upload_files_success(self, file_listener, temp_watch_folder):
        """Test successful file upload."""
        files_to_upload = ["file1.txt", "file2.txt"]

        uploaded_files = await file_listener._upload_files(files_to_upload, temp_watch_folder)

        # Fixed: Now correctly returns list of successfully uploaded filenames
        assert len(uploaded_files) == 2
        assert uploaded_files == ["file1.txt", "file2.txt"]  # Correct behavior: filenames returned

        # Verify S3Manager was called
        assert file_listener.s3_manager.upload_file.call_count == 2

    async def test_upload_files_with_s3_check_skip(self, file_listener, temp_watch_folder):
        """Test upload with S3 existence check - files already exist."""
        # Configure mock to return True for check_exists (file already exists)
        file_listener.s3_manager.check_exists.return_value = True

        files_to_upload = ["file1.txt"]

        uploaded_files = await file_listener._upload_files(files_to_upload, temp_watch_folder)

        # Fixed: Now correctly returns filename when file is skipped (still considered "uploaded")
        # When check_exists returns True, _upload_single_file returns True, so filename is included
        assert len(uploaded_files) == 1
        assert uploaded_files[0] == "file1.txt"  # Correct behavior: filename returned
        file_listener.s3_manager.upload_file.assert_not_called()

    async def test_upload_single_file_success(self, file_listener, temp_watch_folder):
        """Test uploading a single file."""
        filename = "file1.txt"

        result = await file_listener._upload_single_file(filename, temp_watch_folder)

        assert result is True  # Returns True on success (not filename)
        file_listener.s3_manager.upload_file.assert_called_once()

    async def test_upload_single_file_already_exists(self, file_listener, temp_watch_folder):
        """Test uploading a file that already exists in S3."""
        # Configure mock to return True for check_exists
        file_listener.s3_manager.check_exists.return_value = True

        filename = "file1.txt"

        result = await file_listener._upload_single_file(filename, temp_watch_folder)

        assert result is True  # Returns True even when skipped (current behavior)
        file_listener.s3_manager.upload_file.assert_not_called()

    async def test_concurrent_upload_with_semaphore(self, file_listener, temp_watch_folder):
        """Test that concurrent uploads respect semaphore limit."""
        # Create many files to test semaphore
        files_to_upload = [f"file_{i}.txt" for i in range(10)]

        # Create the actual files
        for filename in files_to_upload:
            (temp_watch_folder / filename).write_text(f"Content of {filename}")

        # Upload files
        uploaded_files = await file_listener._upload_files(files_to_upload, temp_watch_folder)

        # All files should be uploaded
        assert len(uploaded_files) == 10

        # Verify semaphore was used (check that upload_file was called for each file)
        assert file_listener.s3_manager.upload_file.call_count == 10

    async def test_partial_upload_backup_info_update(self, file_listener, temp_watch_folder):
        """Test that backup info only includes successfully uploaded files."""
        # Create test files
        test_files = ["success1.txt", "success2.txt", "fail1.txt", "fail2.txt"]
        for filename in test_files:
            (temp_watch_folder / filename).write_text(f"content of {filename}")

        # Configure mock to simulate partial success
        def mock_upload_side_effect(file_path, s3_key):
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

    async def test_should_ignore_file(self, file_listener):
        """Test file ignore patterns."""
        # Test files that should be ignored
        assert file_listener._should_ignore_file(Path(".DS_Store"))
        assert file_listener._should_ignore_file(Path("Thumbs.db"))
        assert file_listener._should_ignore_file(Path(".milo_backup.info"))

        # Test files that should not be ignored
        assert not file_listener._should_ignore_file(Path("normal_file.txt"))
        assert not file_listener._should_ignore_file(Path("document.pdf"))

    async def test_should_ignore_directory(self, file_listener):
        """Test directory ignore patterns."""
        # Test directories that should be ignored
        assert file_listener._should_ignore_directory(Path(".git"))
        assert file_listener._should_ignore_directory(Path("__pycache__"))
        assert file_listener._should_ignore_directory(Path("node_modules"))

        # Test Windows system directories
        assert file_listener._should_ignore_directory(Path("$RECYCLE.BIN"))
        assert file_listener._should_ignore_directory(Path("System Volume Information"))

        # Test directories that should not be ignored
        assert not file_listener._should_ignore_directory(Path("normal_folder"))
        assert not file_listener._should_ignore_directory(Path("Documents"))


class TestFileListenerBackupInfo:
    """Test FileListener backup info management."""

    async def test_load_backup_info_existing_file(self, file_listener, temp_watch_folder):
        """Test loading existing backup info file."""
        # Create a backup info file
        backup_info_file = temp_watch_folder / ".milo_backup.info"
        test_data = {
            "timestamp": "2023-01-01T00:00:00",
            "files": {"file1.txt": "md5_hash_1", "file2.txt": "md5_hash_2"},
        }

        with open(backup_info_file, "w") as f:
            json.dump(test_data, f)

        # Load backup info
        loaded_info = await file_listener._load_backup_info(backup_info_file)

        assert loaded_info == test_data["files"]

    async def test_load_backup_info_nonexistent_file(self, file_listener, temp_watch_folder):
        """Test loading non-existent backup info file."""
        backup_info_file = temp_watch_folder / "nonexistent.milo_backup.info"

        loaded_info = await file_listener._load_backup_info(backup_info_file)

        assert loaded_info == {}

    async def test_update_backup_info(self, file_listener, temp_watch_folder):
        """Test updating backup info file."""
        backup_info_file = temp_watch_folder / ".milo_backup.info"
        current_files = {"file1.txt": "md5_hash_1", "file2.txt": "md5_hash_2"}

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
