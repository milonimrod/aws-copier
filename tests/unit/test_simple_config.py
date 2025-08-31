"""Tests for simple configuration."""

import tempfile
from pathlib import Path

import pytest

from aws_copier.models.simple_config import SimpleConfig


def test_simple_config_creation():
    """Test creating a simple config with defaults."""
    config = SimpleConfig()

    assert config.aws_region == "us-east-1"
    assert config.s3_bucket == "your-bucket-name"
    assert config.max_concurrent_uploads == 100
    assert len(config.watch_folders) == 1
    assert config.discovered_files_folder is not None


def test_simple_config_with_kwargs():
    """Test creating config with custom values."""
    config = SimpleConfig(
        aws_access_key_id="test-key",
        s3_bucket="test-bucket",
        max_concurrent_uploads=50,
        watch_folders=["/tmp", "/home"],
    )

    assert config.aws_access_key_id == "test-key"
    assert config.s3_bucket == "test-bucket"
    assert config.max_concurrent_uploads == 50
    assert len(config.watch_folders) == 2
    assert config.watch_folders[0] == Path("/tmp")


def test_config_save_and_load_yaml():
    """Test saving and loading YAML configuration."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        config_path = Path(f.name)

    try:
        # Create and save config
        original_config = SimpleConfig(
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
            s3_bucket="test-bucket",
            s3_prefix="test-prefix",
            watch_folders=["/test/folder1", "/test/folder2"],
            max_concurrent_uploads=75,
        )

        original_config.save_to_yaml(config_path)

        # Load config
        loaded_config = SimpleConfig.load_from_yaml(config_path)

        # Verify values
        assert loaded_config.aws_access_key_id == "test-access-key"
        assert loaded_config.aws_secret_access_key == "test-secret-key"
        assert loaded_config.s3_bucket == "test-bucket"
        assert loaded_config.s3_prefix == "test-prefix"
        assert loaded_config.max_concurrent_uploads == 75
        assert len(loaded_config.watch_folders) == 2
        assert loaded_config.watch_folders[0] == Path("/test/folder1")

    finally:
        config_path.unlink(missing_ok=True)


def test_config_to_dict():
    """Test converting config to dictionary."""
    config = SimpleConfig(aws_access_key_id="test-key", s3_bucket="test-bucket", watch_folders=["/tmp"])

    config_dict = config.to_dict()

    assert config_dict["aws_access_key_id"] == "test-key"
    assert config_dict["s3_bucket"] == "test-bucket"
    # With new format, watch_folders is now a dict mapping paths to S3 names
    assert config_dict["watch_folders"] == {"/tmp": "tmp"}
    assert config_dict["aws_region"] == "us-east-1"  # default value


def test_config_create_directories():
    """Test creating directories."""
    with tempfile.TemporaryDirectory() as temp_dir:
        discovered_folder = Path(temp_dir) / "test_discovered"

        config = SimpleConfig(discovered_files_folder=str(discovered_folder))

        # Directory should not exist yet
        assert not discovered_folder.exists()

        # Create directories
        config.create_directories()

        # Directory should now exist
        assert discovered_folder.exists()
        assert discovered_folder.is_dir()


def test_yaml_file_not_found():
    """Test loading non-existent YAML file."""
    non_existent_path = Path("/tmp/non_existent_config.yaml")

    with pytest.raises(FileNotFoundError):
        SimpleConfig.load_from_yaml(non_existent_path)


# Tests for new dictionary watch_folders functionality


def test_config_with_dict_watch_folders():
    """Test creating config with dictionary watch_folders format."""
    config = SimpleConfig(
        aws_access_key_id="test-key",
        s3_bucket="test-bucket",
        watch_folders={"/Users/test/Documents": "Documents", "E:/": "Pictures", "/home/user/videos": "Videos"},
    )

    assert config.aws_access_key_id == "test-key"
    assert config.s3_bucket == "test-bucket"
    assert len(config.watch_folders) == 3

    # Check that paths are converted to Path objects
    assert Path("/Users/test/Documents") in config.watch_folders
    assert Path("E:/") in config.watch_folders
    assert Path("/home/user/videos") in config.watch_folders

    # Check the S3 mapping
    assert config.folder_s3_mapping[Path("/Users/test/Documents")] == "Documents"
    assert config.folder_s3_mapping[Path("E:/")] == "Pictures"
    assert config.folder_s3_mapping[Path("/home/user/videos")] == "Videos"


def test_config_with_list_watch_folders_backward_compatibility():
    """Test that list format still works (backward compatibility)."""
    config = SimpleConfig(
        aws_access_key_id="test-key",
        s3_bucket="test-bucket",
        watch_folders=["/Users/test/Documents", "/Users/test/Pictures"],
    )

    assert len(config.watch_folders) == 2
    assert Path("/Users/test/Documents") in config.watch_folders
    assert Path("/Users/test/Pictures") in config.watch_folders

    # Check that default S3 mapping uses folder names
    assert config.folder_s3_mapping[Path("/Users/test/Documents")] == "Documents"
    assert config.folder_s3_mapping[Path("/Users/test/Pictures")] == "Pictures"


def test_get_s3_name_for_folder():
    """Test the get_s3_name_for_folder method."""
    config = SimpleConfig(
        watch_folders={"/Users/test/Documents": "MyDocuments", "E:/": "Pictures", "/tmp": "Temporary"}
    )

    # Test existing mappings
    assert config.get_s3_name_for_folder(Path("/Users/test/Documents")) == "MyDocuments"
    assert config.get_s3_name_for_folder(Path("E:/")) == "Pictures"
    assert config.get_s3_name_for_folder(Path("/tmp")) == "Temporary"

    # Test non-existent path (should return folder name as fallback)
    assert config.get_s3_name_for_folder(Path("/non/existent/path")) == "path"


def test_dict_config_save_and_load_yaml():
    """Test saving and loading YAML configuration with dictionary watch_folders."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        config_path = Path(f.name)

    try:
        # Create and save config with dict format
        original_config = SimpleConfig(
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
            s3_bucket="test-bucket",
            s3_prefix="test-prefix",
            watch_folders={"/test/folder1": "Folder1", "E:/photos": "Pictures", "/home/user/docs": "Documents"},
            max_concurrent_uploads=75,
        )

        original_config.save_to_yaml(config_path)

        # Load config
        loaded_config = SimpleConfig.load_from_yaml(config_path)

        # Verify values
        assert loaded_config.aws_access_key_id == "test-access-key"
        assert loaded_config.aws_secret_access_key == "test-secret-key"
        assert loaded_config.s3_bucket == "test-bucket"
        assert loaded_config.s3_prefix == "test-prefix"
        assert loaded_config.max_concurrent_uploads == 75

        # Verify watch folders and mapping
        assert len(loaded_config.watch_folders) == 3
        assert Path("/test/folder1") in loaded_config.watch_folders
        assert Path("E:/photos") in loaded_config.watch_folders
        assert Path("/home/user/docs") in loaded_config.watch_folders

        # Verify S3 mapping is preserved
        assert loaded_config.folder_s3_mapping[Path("/test/folder1")] == "Folder1"
        assert loaded_config.folder_s3_mapping[Path("E:/photos")] == "Pictures"
        assert loaded_config.folder_s3_mapping[Path("/home/user/docs")] == "Documents"

    finally:
        config_path.unlink(missing_ok=True)


def test_dict_config_to_dict():
    """Test converting dict-format config to dictionary."""
    config = SimpleConfig(
        aws_access_key_id="test-key",
        s3_bucket="test-bucket",
        watch_folders={"/Users/test/Documents": "MyDocuments", "E:/": "Pictures"},
    )

    config_dict = config.to_dict()

    assert config_dict["aws_access_key_id"] == "test-key"
    assert config_dict["s3_bucket"] == "test-bucket"
    # Path normalization may change "E:/" to "E:" on some systems
    expected_watch_folders = {
        "/Users/test/Documents": "MyDocuments",
        str(Path("E:/")): "Pictures",  # Use Path normalization to match actual behavior
    }
    assert config_dict["watch_folders"] == expected_watch_folders
    assert config_dict["aws_region"] == "us-east-1"  # default value


def test_empty_watch_folders():
    """Test config with empty watch_folders."""
    config = SimpleConfig(watch_folders=[])

    assert len(config.watch_folders) == 0
    assert len(config.folder_s3_mapping) == 0


def test_invalid_watch_folders_type():
    """Test config with invalid watch_folders type falls back to default."""
    config = SimpleConfig(watch_folders="invalid_type")

    # Should fall back to default
    assert len(config.watch_folders) == 1
    default_path = Path.home() / "Documents"
    assert config.watch_folders[0] == default_path
    assert config.folder_s3_mapping[default_path] == "Documents"
