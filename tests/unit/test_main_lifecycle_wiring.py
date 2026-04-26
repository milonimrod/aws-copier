"""Tests for main.py AWSCopierApp.start() wiring of CONFIG-07 lifecycle rule and D-10 credential source log."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main as main_module
from main import AWSCopierApp
from aws_copier.models.simple_config import SimpleConfig


def _make_test_config(use_chain: bool) -> SimpleConfig:
    """Create a test SimpleConfig with or without explicit credentials.

    Args:
        use_chain: When True, omit explicit credentials so provider chain is used.

    Returns:
        SimpleConfig instance.
    """
    if use_chain:
        return SimpleConfig(
            aws_region="us-east-1",
            s3_bucket="test-bucket",
            s3_prefix="backup",
            watch_folders=["/tmp/aws-copier-test"],
        )
    return SimpleConfig(
        aws_access_key_id="AKIA",
        aws_secret_access_key="secret",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        s3_prefix="backup",
        watch_folders=["/tmp/aws-copier-test"],
    )


@pytest.fixture
def patched_app_explicit_creds():
    """AWSCopierApp with explicit credentials, all heavy dependencies mocked."""
    cfg = _make_test_config(use_chain=False)
    with (
        patch.object(main_module, "load_config", return_value=cfg),
        patch.object(main_module, "S3Manager") as mock_s3_cls,
        patch.object(main_module, "FileListener") as mock_fl_cls,
        patch.object(main_module, "FolderWatcher") as mock_fw_cls,
    ):
        s3 = AsyncMock()
        s3.initialize = AsyncMock()
        s3.ensure_lifecycle_rule = AsyncMock()
        s3.close = AsyncMock()
        mock_s3_cls.return_value = s3

        fl = AsyncMock()
        fl.scan_all_folders = AsyncMock()
        fl.get_statistics = MagicMock(return_value={})
        fl._active_upload_tasks = set()
        mock_fl_cls.return_value = fl

        fw = AsyncMock()
        fw.start = AsyncMock()
        fw.stop = AsyncMock()
        mock_fw_cls.return_value = fw

        app = AWSCopierApp()
        # Short-circuit the status loop: set shutdown_event BEFORE awaiting start
        app.shutdown_event.set()
        yield app, s3, fl, fw, cfg


@pytest.fixture
def patched_app_chain_creds():
    """AWSCopierApp with provider chain credentials, all heavy dependencies mocked."""
    cfg = _make_test_config(use_chain=True)
    with (
        patch.object(main_module, "load_config", return_value=cfg),
        patch.object(main_module, "S3Manager") as mock_s3_cls,
        patch.object(main_module, "FileListener") as mock_fl_cls,
        patch.object(main_module, "FolderWatcher") as mock_fw_cls,
    ):
        s3 = AsyncMock()
        s3.initialize = AsyncMock()
        s3.ensure_lifecycle_rule = AsyncMock()
        s3.close = AsyncMock()
        mock_s3_cls.return_value = s3

        fl = AsyncMock()
        fl.scan_all_folders = AsyncMock()
        fl.get_statistics = MagicMock(return_value={})
        fl._active_upload_tasks = set()
        mock_fl_cls.return_value = fl

        fw = AsyncMock()
        fw.start = AsyncMock()
        fw.stop = AsyncMock()
        mock_fw_cls.return_value = fw

        app = AWSCopierApp()
        app.shutdown_event.set()
        yield app, s3, fl, fw, cfg


class TestStartupWiring:
    """CONFIG-07 + D-10: ensure_lifecycle_rule and credential_source log are wired into start()."""

    async def test_start_calls_ensure_lifecycle_rule(self, patched_app_explicit_creds):
        """ensure_lifecycle_rule is awaited during startup."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        await app.start()
        s3.initialize.assert_awaited_once()
        s3.ensure_lifecycle_rule.assert_awaited_once()

    async def test_initialize_called_before_ensure_lifecycle_rule(self, patched_app_explicit_creds):
        """initialize is called before ensure_lifecycle_rule (ordering guarantee)."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        call_order: list = []

        async def _init():
            call_order.append("initialize")

        async def _ensure():
            call_order.append("ensure_lifecycle_rule")

        s3.initialize.side_effect = _init
        s3.ensure_lifecycle_rule.side_effect = _ensure
        await app.start()
        assert call_order.index("initialize") < call_order.index("ensure_lifecycle_rule")

    async def test_ensure_lifecycle_rule_called_before_scan_all_folders(self, patched_app_explicit_creds):
        """ensure_lifecycle_rule is called before the initial scan (CONFIG-07 ordering)."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        call_order: list = []

        async def _ensure():
            call_order.append("ensure_lifecycle_rule")

        async def _scan():
            call_order.append("scan_all_folders")

        s3.ensure_lifecycle_rule.side_effect = _ensure
        fl.scan_all_folders.side_effect = _scan
        await app.start()
        assert call_order.index("ensure_lifecycle_rule") < call_order.index("scan_all_folders")

    async def test_logs_credential_source_config_yaml(self, patched_app_explicit_creds, caplog):
        """D-10: startup logs 'AWS credentials loaded from: config.yaml' when explicit creds are present."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        assert cfg.credential_source == "config.yaml"
        with caplog.at_level(logging.INFO):
            await app.start()
        assert any(
            "AWS credentials loaded from: config.yaml" in r.message
            for r in caplog.records
        )

    async def test_logs_credential_source_provider_chain(self, patched_app_chain_creds, caplog):
        """D-10: startup logs provider chain source when no explicit creds in config."""
        app, s3, fl, fw, cfg = patched_app_chain_creds
        assert cfg.use_credential_chain is True
        with caplog.at_level(logging.INFO):
            await app.start()
        assert any(
            "AWS credentials loaded from: provider chain (env / ~/.aws/credentials / IAM)"
            in r.message
            for r in caplog.records
        )

    async def test_start_does_not_crash_when_ensure_lifecycle_rule_returns_none(self, patched_app_explicit_creds):
        """Startup completes without error when ensure_lifecycle_rule returns None (D-11 best-effort)."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        s3.ensure_lifecycle_rule.return_value = None
        # Must complete without raising
        await app.start()
