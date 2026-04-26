"""Tests for S3Manager Phase 2 changes: PERF-03, CONFIG-05 client wiring, and CONFIG-07."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig


@pytest.fixture
def explicit_creds_config():
    """Config with explicit AWS credentials (use_credential_chain=False)."""
    return SimpleConfig(
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        s3_prefix="test-prefix",
    )


@pytest.fixture
def chain_config():
    """Config without explicit credentials — use_credential_chain=True."""
    return SimpleConfig(
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        s3_prefix="test-prefix",
    )


class TestPrecomputedMd5:
    """PERF-03: upload_file uses precomputed_md5 when provided."""

    async def test_upload_uses_precomputed_md5(self, explicit_creds_config, tmp_path):
        """When precomputed_md5 is supplied, _calculate_md5 is NOT called."""
        local = tmp_path / "f.txt"
        local.write_bytes(b"data")
        s3 = S3Manager(explicit_creds_config)
        mock_client = AsyncMock()
        mock_client.put_object.return_value = {"ETag": "x"}
        mock_client.head_object.return_value = {
            "Metadata": {"md5-checksum": "ff" * 16},
            "ETag": '"some_etag"',
        }
        with patch.object(s3, "_get_or_create_client", return_value=mock_client), \
             patch.object(s3, "_calculate_md5", new_callable=AsyncMock) as spy_md5:
            ok = await s3.upload_file(local, "key", precomputed_md5="ff" * 16)
            assert ok is True
            spy_md5.assert_not_called()

    async def test_upload_recomputes_when_omitted(self, explicit_creds_config, tmp_path):
        """When precomputed_md5 is NOT supplied, _calculate_md5 IS called exactly once."""
        local = tmp_path / "f.txt"
        local.write_bytes(b"data")
        s3 = S3Manager(explicit_creds_config)
        mock_client = AsyncMock()
        mock_client.put_object.return_value = {"ETag": "x"}
        mock_client.head_object.return_value = {
            "Metadata": {"md5-checksum": "abc"},
            "ETag": '"some_etag"',
        }
        with patch.object(s3, "_get_or_create_client", return_value=mock_client), \
             patch.object(s3, "_calculate_md5", new_callable=AsyncMock, return_value="abc") as spy_md5:
            ok = await s3.upload_file(local, "key")
            assert ok is True
            spy_md5.assert_awaited_once()

    async def test_upload_passes_precomputed_md5_into_metadata(self, explicit_creds_config, tmp_path):
        """Metadata stored in S3 must use the precomputed value when supplied."""
        local = tmp_path / "f.txt"
        local.write_bytes(b"data")
        expected_md5 = "aa" * 16
        s3 = S3Manager(explicit_creds_config)
        mock_client = AsyncMock()
        mock_client.put_object.return_value = {"ETag": "x"}
        mock_client.head_object.return_value = {
            "Metadata": {"md5-checksum": expected_md5},
            "ETag": '"some_etag"',
        }
        with patch.object(s3, "_get_or_create_client", return_value=mock_client), \
             patch.object(s3, "_calculate_md5", new_callable=AsyncMock):
            await s3.upload_file(local, "key", precomputed_md5=expected_md5)
            # Confirm put_object was called with the expected md5 in Metadata
            call_kwargs = mock_client.put_object.call_args[1]
            assert call_kwargs["Metadata"]["md5-checksum"] == expected_md5


class TestCredentialChainClientWiring:
    """CONFIG-05 (client side): credential chain controls create_client kwargs."""

    async def test_get_or_create_client_omits_creds_when_chain_active(self, chain_config):
        """When use_credential_chain=True, create_client is called WITHOUT explicit creds."""
        s3 = S3Manager(chain_config)
        assert chain_config.use_credential_chain is True
        fake_client = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=fake_client)
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(s3._session, "create_client", return_value=cm) as mock_create:
            await s3._get_or_create_client()
            _, kwargs = mock_create.call_args
            assert "aws_access_key_id" not in kwargs
            assert "aws_secret_access_key" not in kwargs
            assert kwargs["region_name"] == "us-east-1"

    async def test_get_or_create_client_passes_creds_when_chain_inactive(self, explicit_creds_config):
        """When use_credential_chain=False, create_client IS called WITH explicit creds."""
        s3 = S3Manager(explicit_creds_config)
        assert explicit_creds_config.use_credential_chain is False
        fake_client = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=fake_client)
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(s3._session, "create_client", return_value=cm) as mock_create:
            await s3._get_or_create_client()
            _, kwargs = mock_create.call_args
            assert kwargs["aws_access_key_id"] == "test-access-key"
            assert kwargs["aws_secret_access_key"] == "test-secret-key"

    async def test_initialize_uses_chain_aware_kwargs(self, chain_config):
        """initialize() also omits explicit creds when use_credential_chain=True."""
        s3 = S3Manager(chain_config)
        fake_client = AsyncMock()
        fake_client.head_bucket.return_value = {}
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=fake_client)
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(s3._session, "create_client", return_value=cm) as mock_create:
            await s3.initialize()
            _, kwargs = mock_create.call_args
            assert "aws_access_key_id" not in kwargs
            assert "aws_secret_access_key" not in kwargs


class TestEnsureLifecycleRule:
    """CONFIG-07: ensure_lifecycle_rule covers all D-11 / D-12 branches."""

    def _make_client_error(self, code: str, op: str = "GetBucketLifecycleConfiguration") -> ClientError:
        """Helper to build a ClientError with a given error code."""
        return ClientError({"Error": {"Code": code, "Message": ""}}, op)

    async def test_creates_rule_when_no_lifecycle_config(self, explicit_creds_config):
        """When NoSuchLifecycleConfiguration is raised, put_bucket_lifecycle_configuration is called."""
        s3 = S3Manager(explicit_creds_config)
        mock_client = AsyncMock()
        mock_client.get_bucket_lifecycle_configuration.side_effect = self._make_client_error(
            "NoSuchLifecycleConfiguration"
        )
        mock_client.put_bucket_lifecycle_configuration.return_value = {}
        with patch.object(s3, "_get_or_create_client", return_value=mock_client):
            await s3.ensure_lifecycle_rule()
        mock_client.put_bucket_lifecycle_configuration.assert_awaited_once()
        args, kwargs = mock_client.put_bucket_lifecycle_configuration.call_args
        cfg = kwargs["LifecycleConfiguration"]
        assert cfg["Rules"][0]["ID"] == "aws-copier-abort-incomplete-multipart"
        assert cfg["Rules"][0]["Status"] == "Enabled"
        assert cfg["Rules"][0]["Filter"] == {"Prefix": ""}
        assert cfg["Rules"][0]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 1

    async def test_skips_when_abort_rule_already_present(self, explicit_creds_config, caplog):
        """When an AbortIncompleteMultipartUpload rule already exists, put is NOT called."""
        s3 = S3Manager(explicit_creds_config)
        mock_client = AsyncMock()
        mock_client.get_bucket_lifecycle_configuration.return_value = {
            "Rules": [
                {
                    "ID": "user-rule",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                }
            ]
        }
        with patch.object(s3, "_get_or_create_client", return_value=mock_client):
            with caplog.at_level("INFO"):
                await s3.ensure_lifecycle_rule()
        mock_client.put_bucket_lifecycle_configuration.assert_not_awaited()
        assert any("already present" in r.message and "DaysAfterInitiation=7" in r.message for r in caplog.records)

    async def test_warns_and_skips_when_other_rules_but_no_abort(self, explicit_creds_config, caplog):
        """When other lifecycle rules exist but no AbortIncomplete, warn and skip (D-12)."""
        s3 = S3Manager(explicit_creds_config)
        mock_client = AsyncMock()
        mock_client.get_bucket_lifecycle_configuration.return_value = {
            "Rules": [
                {
                    "ID": "transition-rule",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}],
                }
            ]
        }
        with patch.object(s3, "_get_or_create_client", return_value=mock_client):
            with caplog.at_level("WARNING"):
                await s3.ensure_lifecycle_rule()
        mock_client.put_bucket_lifecycle_configuration.assert_not_awaited()
        assert any("Could not verify multipart lifecycle rule" in r.message for r in caplog.records)

    async def test_warns_and_returns_on_other_client_error(self, explicit_creds_config, caplog):
        """When a ClientError other than NoSuchLifecycleConfiguration is raised, warn and return."""
        s3 = S3Manager(explicit_creds_config)
        mock_client = AsyncMock()
        mock_client.get_bucket_lifecycle_configuration.side_effect = self._make_client_error("AccessDenied")
        with patch.object(s3, "_get_or_create_client", return_value=mock_client):
            with caplog.at_level("WARNING"):
                await s3.ensure_lifecycle_rule()  # must not raise
        mock_client.put_bucket_lifecycle_configuration.assert_not_awaited()
        assert any("Could not verify multipart lifecycle rule" in r.message for r in caplog.records)

    async def test_warns_when_put_fails(self, explicit_creds_config, caplog):
        """When put_bucket_lifecycle_configuration raises, warn and return (D-11)."""
        s3 = S3Manager(explicit_creds_config)
        mock_client = AsyncMock()
        mock_client.get_bucket_lifecycle_configuration.side_effect = self._make_client_error(
            "NoSuchLifecycleConfiguration"
        )
        mock_client.put_bucket_lifecycle_configuration.side_effect = self._make_client_error(
            "AccessDenied", op="PutBucketLifecycleConfiguration"
        )
        with patch.object(s3, "_get_or_create_client", return_value=mock_client):
            with caplog.at_level("WARNING"):
                await s3.ensure_lifecycle_rule()  # must not raise
        assert any("Could not verify multipart lifecycle rule" in r.message for r in caplog.records)

    async def test_does_not_raise_on_unexpected_exception(self, explicit_creds_config):
        """When an unexpected Exception is raised by get, it must not propagate (D-11)."""
        s3 = S3Manager(explicit_creds_config)
        mock_client = AsyncMock()
        mock_client.get_bucket_lifecycle_configuration.side_effect = Exception("boom")
        with patch.object(s3, "_get_or_create_client", return_value=mock_client):
            # Must not raise — D-11
            await s3.ensure_lifecycle_rule()
