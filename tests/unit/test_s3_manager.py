"""Tests for S3 manager."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig


@pytest.fixture
def test_config():
    """Create a test configuration."""
    return SimpleConfig(
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        s3_prefix="test-prefix",
    )


@pytest.fixture
def s3_manager(test_config):
    """Create an S3 manager for testing."""
    return S3Manager(test_config)


def test_s3_manager_initialization(s3_manager, test_config):
    """Test S3 manager initialization."""
    assert s3_manager.config == test_config
    assert s3_manager._session is not None  # Session is now created in __init__
    assert s3_manager._s3_client is None  # Client is still None until _get_or_create_client
    assert s3_manager._exit_stack is not None  # AsyncExitStack is created in __init__


def test_build_s3_key_with_prefix(s3_manager):
    """Test building S3 key with prefix."""
    s3_key = s3_manager._build_s3_key("file.txt")
    assert s3_key == "test-prefix/file.txt"


def test_build_s3_key_without_prefix():
    """Test building S3 key without prefix."""
    config = SimpleConfig(s3_prefix="")
    manager = S3Manager(config)

    s3_key = manager._build_s3_key("file.txt")
    assert s3_key == "file.txt"


def test_build_s3_key_with_trailing_slash():
    """Test building S3 key with prefix that has trailing slash."""
    config = SimpleConfig(s3_prefix="test-prefix/")
    manager = S3Manager(config)

    s3_key = manager._build_s3_key("file.txt")
    assert s3_key == "test-prefix/file.txt"


@pytest.mark.asyncio
async def test_calculate_md5(s3_manager):
    """Test MD5 calculation."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write("test content")
        test_file = Path(f.name)

    try:
        md5_hash = await s3_manager._calculate_md5(test_file)

        # "test content" should have a specific MD5
        assert md5_hash is not None
        assert len(md5_hash) == 32  # MD5 is 32 hex characters
        assert md5_hash == "9473fdd0d880a43c21b7778d34872157"  # MD5 of "test content"

    finally:
        test_file.unlink()


@pytest.mark.asyncio
async def test_calculate_md5_nonexistent_file(s3_manager):
    """Test MD5 calculation for non-existent file."""
    non_existent_file = Path("/tmp/non_existent_file.txt")

    md5_hash = await s3_manager._calculate_md5(non_existent_file)
    assert md5_hash is None


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_initialize_success(mock_get_session, test_config):
    """Test successful S3 manager initialization."""
    # Mock the session and client
    mock_session = MagicMock()
    mock_s3_client = AsyncMock()
    mock_s3_client.__aenter__ = AsyncMock(return_value=mock_s3_client)
    mock_s3_client.__aexit__ = AsyncMock(return_value=None)

    mock_session.create_client.return_value = mock_s3_client
    mock_get_session.return_value = mock_session

    # Create S3Manager after mocking
    s3_manager = S3Manager(test_config)
    await s3_manager.initialize()

    # Verify session and client were created
    mock_get_session.assert_called_once()
    mock_session.create_client.assert_called_once_with(
        "s3",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        region_name="us-east-1",
        config=s3_manager._client_config,
    )

    # Verify head_bucket was called to test connection
    mock_s3_client.head_bucket.assert_called_once_with(Bucket="test-bucket")


@pytest.mark.asyncio
async def test_close(s3_manager):
    """Test closing S3 manager."""
    # Mock the client and exit stack
    mock_client = AsyncMock()
    mock_exit_stack = AsyncMock()
    s3_manager._s3_client = mock_client
    s3_manager._exit_stack = mock_exit_stack

    await s3_manager.close()

    # In the new AsyncExitStack pattern, close() calls client.close() and exit_stack.aclose()
    mock_client.close.assert_called_once()
    mock_exit_stack.aclose.assert_called_once()

    # Verify cleanup
    assert s3_manager._s3_client is None
    assert s3_manager._exit_stack is None


@pytest.mark.asyncio
async def test_upload_file_not_exists(s3_manager):
    """Test uploading a file that doesn't exist."""
    non_existent_file = Path("/tmp/non_existent_file.txt")

    result = await s3_manager.upload_file(non_existent_file, "test.txt")
    assert result is False


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_check_exists_file_not_found(mock_get_session, s3_manager):
    """Test checking existence of file that doesn't exist in S3."""
    from botocore.exceptions import ClientError

    # Mock the session and client
    mock_session = MagicMock()
    mock_s3_client = AsyncMock()
    mock_s3_client.__aenter__ = AsyncMock(return_value=mock_s3_client)
    mock_s3_client.__aexit__ = AsyncMock(return_value=None)

    # Mock 404 error
    error_response = {"Error": {"Code": "404"}}
    mock_s3_client.head_object.side_effect = ClientError(error_response, "HeadObject")

    mock_session.create_client.return_value = mock_s3_client
    mock_get_session.return_value = mock_session

    s3_manager._s3_client = mock_s3_client

    result = await s3_manager.check_exists("test.txt")
    assert result is False


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_check_exists_file_found(mock_get_session, s3_manager):
    """Test checking existence of file that exists in S3."""
    # Mock the session and client
    mock_session = MagicMock()
    mock_s3_client = AsyncMock()
    mock_s3_client.__aenter__ = AsyncMock(return_value=mock_s3_client)
    mock_s3_client.__aexit__ = AsyncMock(return_value=None)

    # Mock successful response
    mock_s3_client.head_object.return_value = {
        "Metadata": {},
        "ETag": '"d41d8cd98f00b204e9800998ecf8427e"',
        "ContentLength": 0,
    }

    mock_session.create_client.return_value = mock_s3_client
    mock_get_session.return_value = mock_session

    # Set the client directly for the persistent pattern
    s3_manager._s3_client = mock_s3_client

    result = await s3_manager.check_exists("test.txt")
    assert result is True


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_check_exists_with_md5_match(mock_get_session, s3_manager):
    """Test checking existence with MD5 verification - match."""
    # Mock the session and client
    mock_session = MagicMock()
    mock_s3_client = AsyncMock()
    mock_s3_client.__aenter__ = AsyncMock(return_value=mock_s3_client)
    mock_s3_client.__aexit__ = AsyncMock(return_value=None)

    # Mock successful response with matching MD5
    test_md5 = "d41d8cd98f00b204e9800998ecf8427e"
    mock_s3_client.head_object.return_value = {
        "Metadata": {"md5-checksum": test_md5},
        "ETag": f'"{test_md5}"',
        "ContentLength": 0,
    }

    mock_session.create_client.return_value = mock_s3_client
    mock_get_session.return_value = mock_session

    # Set the client directly for the persistent pattern
    s3_manager._s3_client = mock_s3_client

    result = await s3_manager.check_exists("test.txt", test_md5)
    assert result is True


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_check_exists_with_md5_mismatch(mock_get_session, s3_manager):
    """Test checking existence with MD5 verification - mismatch."""
    # Mock the session and client
    mock_session = MagicMock()
    mock_s3_client = AsyncMock()
    mock_s3_client.__aenter__ = AsyncMock(return_value=mock_s3_client)
    mock_s3_client.__aexit__ = AsyncMock(return_value=None)

    # Mock successful response with different MD5
    stored_md5 = "d41d8cd98f00b204e9800998ecf8427e"
    expected_md5 = "different_md5_hash_value_here"
    mock_s3_client.head_object.return_value = {
        "Metadata": {"md5-checksum": stored_md5},
        "ETag": f'"{stored_md5}"',
        "ContentLength": 0,
    }

    mock_session.create_client.return_value = mock_s3_client
    mock_get_session.return_value = mock_session

    s3_manager._s3_client = mock_s3_client

    result = await s3_manager.check_exists("test.txt", expected_md5)
    assert result is False
