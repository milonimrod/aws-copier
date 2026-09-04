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


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_delete_object_success(mock_get_session, s3_manager):
    """delete_object() issues DeleteObject with the fully-prefixed key and reports success."""
    mock_session = MagicMock()
    mock_s3_client = AsyncMock()
    mock_s3_client.__aenter__ = AsyncMock(return_value=mock_s3_client)
    mock_s3_client.__aexit__ = AsyncMock(return_value=None)

    mock_session.create_client.return_value = mock_s3_client
    mock_get_session.return_value = mock_session
    s3_manager._s3_client = mock_s3_client

    result = await s3_manager.delete_object("old/file.txt")

    assert result is True
    mock_s3_client.delete_object.assert_awaited_once_with(Bucket="test-bucket", Key="test-prefix/old/file.txt")


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_delete_object_failure_returns_false(mock_get_session, s3_manager):
    """delete_object() returns False (never raises) when the S3 call fails."""
    mock_session = MagicMock()
    mock_s3_client = AsyncMock()
    mock_s3_client.__aenter__ = AsyncMock(return_value=mock_s3_client)
    mock_s3_client.__aexit__ = AsyncMock(return_value=None)
    mock_s3_client.delete_object.side_effect = Exception("boom")

    mock_session.create_client.return_value = mock_s3_client
    mock_get_session.return_value = mock_session
    s3_manager._s3_client = mock_s3_client

    result = await s3_manager.delete_object("old/file.txt")

    assert result is False


@pytest.mark.asyncio
async def test_soft_delete_object_moves_to_trash_prefix(s3_manager):
    """soft_delete_object() is a thin wrapper: move to _trash/<key> via move_object()."""
    s3_manager.move_object = AsyncMock(return_value=True)

    result = await s3_manager.soft_delete_object("Pictures/album/photo.jpg")

    assert result is True
    s3_manager.move_object.assert_awaited_once_with("Pictures/album/photo.jpg", "_trash/Pictures/album/photo.jpg")


@pytest.mark.asyncio
async def test_soft_delete_object_propagates_move_failure(s3_manager):
    s3_manager.move_object = AsyncMock(return_value=False)

    result = await s3_manager.soft_delete_object("Pictures/album/photo.jpg")

    assert result is False


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_ensure_trash_lifecycle_rule_creates_when_none_exists(mock_get_session, s3_manager):
    from botocore.exceptions import ClientError

    mock_s3_client = AsyncMock()
    mock_s3_client.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": "NoSuchLifecycleConfiguration"}}, "GetBucketLifecycleConfiguration"
    )
    mock_get_session.return_value = MagicMock()
    s3_manager._s3_client = mock_s3_client

    await s3_manager.ensure_trash_lifecycle_rule(expiration_days=30)

    mock_s3_client.put_bucket_lifecycle_configuration.assert_awaited_once()
    kwargs = mock_s3_client.put_bucket_lifecycle_configuration.await_args.kwargs
    rules = kwargs["LifecycleConfiguration"]["Rules"]
    assert len(rules) == 1
    assert rules[0]["ID"] == "aws-copier-trash-expiration"
    assert rules[0]["Filter"] == {"Prefix": "_trash/"}
    assert rules[0]["Expiration"] == {"Days": 30}


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_ensure_trash_lifecycle_rule_preserves_existing_rules(mock_get_session, s3_manager):
    """Regression: must APPEND, never replace — put_bucket_lifecycle_configuration
    overwrites the whole rule set, so an existing (e.g. multipart-abort) rule must survive."""
    existing_rule = {"ID": "aws-copier-abort-incomplete-multipart", "Status": "Enabled"}
    mock_s3_client = AsyncMock()
    mock_s3_client.get_bucket_lifecycle_configuration.return_value = {"Rules": [existing_rule]}
    mock_get_session.return_value = MagicMock()
    s3_manager._s3_client = mock_s3_client

    await s3_manager.ensure_trash_lifecycle_rule()

    kwargs = mock_s3_client.put_bucket_lifecycle_configuration.await_args.kwargs
    rules = kwargs["LifecycleConfiguration"]["Rules"]
    assert existing_rule in rules
    assert any(r["ID"] == "aws-copier-trash-expiration" for r in rules)


@pytest.mark.asyncio
@patch("aws_copier.core.s3_manager.get_session")
async def test_ensure_trash_lifecycle_rule_is_idempotent(mock_get_session, s3_manager):
    """Already present — must not call put (never overwrite unnecessarily)."""
    mock_s3_client = AsyncMock()
    mock_s3_client.get_bucket_lifecycle_configuration.return_value = {
        "Rules": [{"ID": "aws-copier-trash-expiration", "Status": "Enabled"}]
    }
    mock_get_session.return_value = MagicMock()
    s3_manager._s3_client = mock_s3_client

    await s3_manager.ensure_trash_lifecycle_rule()

    mock_s3_client.put_bucket_lifecycle_configuration.assert_not_awaited()
