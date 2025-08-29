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
    assert config_dict["watch_folders"] == ["/tmp"]
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
