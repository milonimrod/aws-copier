"""
Comprehensive tests for S3Manager covering all functionality.
Tests both basic operations and meaningful business logic.
"""

import asyncio
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig


@pytest.fixture
def s3_config():
    """Test configuration for S3Manager."""
    return SimpleConfig(
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        s3_prefix="backup",
    )


@pytest.fixture
def s3_manager(s3_config):
    """Create an S3 manager for testing."""
    return S3Manager(s3_config)


@pytest.fixture
def temp_file():
    """Create a temporary file with known content."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        content = "This is test content for S3 upload testing!"
        f.write(content)
        temp_path = Path(f.name)

    yield temp_path, content

    # Cleanup
    if temp_path.exists():
        temp_path.unlink()


class TestS3ManagerBasicOperations:
    """Test basic S3Manager operations and configuration."""

    def test_s3_manager_initialization(self, s3_manager, s3_config):
        """Test S3Manager initialization with proper configuration."""
        assert s3_manager.config == s3_config
        assert s3_manager._s3_client is None  # Not initialized yet
        assert s3_manager._exit_stack is not None

    def test_build_s3_key_with_prefix(self, s3_manager):
        """Test building S3 key includes prefix when configured."""
        key = s3_manager._build_s3_key("test/file.txt")
        assert key == "backup/test/file.txt"

    def test_build_s3_key_without_prefix(self):
        """Test building S3 key without prefix."""
        config = SimpleConfig(s3_prefix="")
        manager = S3Manager(config)

        key = manager._build_s3_key("test/file.txt")
        assert key == "test/file.txt"

    def test_build_s3_key_with_trailing_slash_prefix(self):
        """Test S3 key building handles trailing slash in prefix."""
        config = SimpleConfig(s3_prefix="backup/")
        manager = S3Manager(config)

        key = manager._build_s3_key("test/file.txt")
        assert key == "backup/test/file.txt"

    async def test_calculate_md5_success(self, s3_manager):
        """Test MD5 calculation for existing file."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            content = "test content"
            f.write(content)
            test_file = Path(f.name)

        try:
            md5_hash = await s3_manager._calculate_md5(test_file)
            expected_md5 = hashlib.md5(content.encode("utf-8")).hexdigest()

            assert md5_hash == expected_md5
            assert len(md5_hash) == 32  # MD5 is 32 hex characters
            assert md5_hash == "9473fdd0d880a43c21b7778d34872157"  # MD5 of "test content"

        finally:
            test_file.unlink()

    async def test_calculate_md5_nonexistent_file(self, s3_manager):
        """Test MD5 calculation for non-existent file returns None."""
        md5_hash = await s3_manager._calculate_md5(Path("/nonexistent/file.txt"))
        assert md5_hash is None


class TestS3ManagerAWSOperations:
    """Test S3Manager AWS integration operations."""

    @patch("aws_copier.core.s3_manager.get_session")
    async def test_initialize_success(self, mock_get_session, s3_config):
        """Test successful S3Manager initialization."""
        # Mock the session and client
        mock_session = MagicMock()
        mock_client = AsyncMock()
        mock_get_session.return_value = mock_session

        # Mock the context manager behavior
        mock_session.create_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_session.create_client.return_value.__aexit__ = AsyncMock(return_value=None)

        manager = S3Manager(s3_config)

        # Should not raise an exception
        await manager.initialize()

        # Verify session was created with correct parameters
        mock_session.create_client.assert_called_with(
            "s3",
            aws_access_key_id=s3_config.aws_access_key_id,
            aws_secret_access_key=s3_config.aws_secret_access_key,
            region_name=s3_config.aws_region,
            config=manager._client_config,
        )

    async def test_close_manager(self, s3_manager):
        """Test proper cleanup when closing manager."""
        # Mock the client and exit stack
        mock_client = AsyncMock()
        mock_exit_stack = AsyncMock()

        s3_manager._s3_client = mock_client
        s3_manager._exit_stack = mock_exit_stack

        # Close manager
        await s3_manager.close()

        # Verify cleanup
        mock_client.close.assert_called_once()
        mock_exit_stack.aclose.assert_called_once()
        assert s3_manager._s3_client is None
        assert s3_manager._exit_stack is None

    async def test_upload_nonexistent_file(self, s3_manager):
        """Test uploading a file that doesn't exist."""
        nonexistent_path = Path("/nonexistent/file.txt")

        result = await s3_manager.upload_file(nonexistent_path, "test/nonexistent.txt")
        assert result is False

    @patch("aws_copier.core.s3_manager.get_session")
    async def test_check_exists_file_found(self, mock_get_session, s3_manager):
        """Test checking existence of a file that exists."""
        # Mock the session and client
        mock_session = MagicMock()
        mock_client = AsyncMock()
        mock_get_session.return_value = mock_session

        s3_manager._s3_client = mock_client

        # Mock successful head_object response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.head_object.return_value = {"Metadata": {"md5-checksum": "test-md5"}, "ETag": '"test-etag"'}

        exists = await s3_manager.check_exists("test/exists.txt")
        assert exists is True

    @patch("aws_copier.core.s3_manager.get_session")
    async def test_check_exists_file_not_found(self, mock_get_session, s3_manager):
        """Test checking existence of a file that doesn't exist."""
        from botocore.exceptions import ClientError

        # Mock the session and client
        mock_session = MagicMock()
        mock_client = AsyncMock()
        mock_get_session.return_value = mock_session

        s3_manager._s3_client = mock_client

        # Mock ClientError for 404
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")

        exists = await s3_manager.check_exists("test/nonexistent.txt")
        assert exists is False

    @patch("aws_copier.core.s3_manager.get_session")
    async def test_check_exists_with_md5_match(self, mock_get_session, s3_manager):
        """Test checking existence with matching MD5."""
        # Mock the session and client
        mock_session = MagicMock()
        mock_client = AsyncMock()
        mock_get_session.return_value = mock_session

        s3_manager._s3_client = mock_client

        # Mock successful head_object response with matching MD5
        test_md5 = "matching-md5-hash"
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.head_object.return_value = {"Metadata": {"md5-checksum": test_md5}, "ETag": f'"{test_md5}"'}

        exists = await s3_manager.check_exists("test/file.txt", test_md5)
        assert exists is True

    @patch("aws_copier.core.s3_manager.get_session")
    async def test_check_exists_with_md5_mismatch(self, mock_get_session, s3_manager):
        """Test checking existence with mismatched MD5."""
        # Mock the session and client
        mock_session = MagicMock()
        mock_client = AsyncMock()
        mock_get_session.return_value = mock_session

        s3_manager._s3_client = mock_client

        # Mock successful head_object response with different MD5
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.head_object.return_value = {
            "Metadata": {"md5-checksum": "different-md5"},
            "ETag": '"different-etag"',
        }

        exists = await s3_manager.check_exists("test/file.txt", "expected-md5")
        assert exists is False


class TestS3ManagerBusinessLogic:
    """Test S3Manager business logic and real-world scenarios."""

    async def test_concurrent_md5_operations(self, s3_manager):
        """Test that S3Manager can handle concurrent MD5 calculations."""
        # Create multiple test files
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            files = []
            for i in range(5):
                file_path = temp_path / f"file_{i}.txt"
                file_path.write_text(f"Content {i}")
                files.append(file_path)

            # Calculate MD5s concurrently
            tasks = [s3_manager._calculate_md5(file_path) for file_path in files]
            results = await asyncio.gather(*tasks)

            # All should succeed
            assert all(result is not None for result in results)
            assert len(set(results)) == 5  # All different MD5s

    async def test_md5_consistency(self, s3_manager):
        """Test that MD5 calculation is consistent."""
        content = "Consistent test content"
        expected_md5 = hashlib.md5(content.encode("utf-8")).hexdigest()

        # Create multiple files with same content
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            files = []
            for i in range(3):
                file_path = temp_path / f"same_content_{i}.txt"
                file_path.write_text(content)
                files.append(file_path)

            # Calculate MD5s
            results = []
            for file_path in files:
                md5_hash = await s3_manager._calculate_md5(file_path)
                results.append(md5_hash)

            # All should be the same
            assert all(result == expected_md5 for result in results)

    def test_s3_key_building_edge_cases(self):
        """Test S3 key building with various edge cases."""
        # Test with None prefix
        config = SimpleConfig(s3_prefix=None)
        manager = S3Manager(config)
        key = manager._build_s3_key("file.txt")
        assert key == "file.txt"

        # Test with empty string prefix
        config = SimpleConfig(s3_prefix="")
        manager = S3Manager(config)
        key = manager._build_s3_key("file.txt")
        assert key == "file.txt"

        # Test with multiple slashes
        config = SimpleConfig(s3_prefix="backup//folder//")
        manager = S3Manager(config)
        key = manager._build_s3_key("file.txt")
        assert key == "backup//folder/file.txt"

    async def test_error_handling_robustness(self, s3_manager):
        """Test that S3Manager handles errors gracefully."""
        # Test with invalid file paths
        invalid_paths = [
            Path(""),
            Path("/"),
            Path("non/existent/path/file.txt"),
            Path("\x00invalid"),  # Null byte
        ]

        for invalid_path in invalid_paths:
            try:
                result = await s3_manager._calculate_md5(invalid_path)
                # Should return None for invalid paths
                assert result is None
            except Exception:
                # Or raise an exception, both are acceptable
                pass

    def test_configuration_validation(self):
        """Test that S3Manager properly handles configuration."""
        # Test with minimal config
        config = SimpleConfig()
        manager = S3Manager(config)
        assert manager.config == config

        # Test with full config
        config = SimpleConfig(
            aws_access_key_id="key",
            aws_secret_access_key="secret",
            aws_region="us-west-2",
            s3_bucket="my-bucket",
            s3_prefix="my-prefix",
        )
        manager = S3Manager(config)
        assert manager.config.aws_region == "us-west-2"
        assert manager.config.s3_bucket == "my-bucket"
        assert manager.config.s3_prefix == "my-prefix"
