"""Tests for main.py AWSCopierApp.start() wiring of CONFIG-07 lifecycle rule and D-10 credential source log."""

import logging
from pathlib import Path
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
        fl.clear_caches = MagicMock()
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
        fl.clear_caches = MagicMock()
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


class TestMemoryHousekeeping:
    """MEM-01: the status loop clears the backup-info cache and trims freed heap each pass."""

    async def test_status_loop_clears_file_listener_caches(self, patched_app_explicit_creds):
        """clear_caches() is called during the (short-circuited, single-pass) status loop."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        await app.start()
        fl.clear_caches.assert_called_once()

    async def test_status_loop_calls_trim_memory(self, patched_app_explicit_creds):
        """_trim_memory() is invoked each status loop pass."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        with patch.object(main_module, "_trim_memory") as mock_trim:
            await app.start()
        mock_trim.assert_called_once()

    def test_trim_memory_noop_on_non_linux(self):
        """_trim_memory does nothing (no ctypes/libc call) on platforms without glibc."""
        with (
            patch.object(main_module.sys, "platform", "darwin"),
            patch.object(main_module.ctypes, "CDLL") as mock_cdll,
        ):
            main_module._trim_memory()
        mock_cdll.assert_not_called()

    def test_trim_memory_calls_malloc_trim_on_linux(self):
        """_trim_memory calls libc's malloc_trim(0) on Linux."""
        mock_libc = MagicMock()
        with (
            patch.object(main_module.sys, "platform", "linux"),
            patch.object(main_module.ctypes, "CDLL", return_value=mock_libc) as mock_cdll,
        ):
            main_module._trim_memory()
        mock_cdll.assert_called_once_with("libc.so.6")
        mock_libc.malloc_trim.assert_called_once_with(0)

    def test_trim_memory_swallows_errors(self):
        """_trim_memory never raises, even if libc/malloc_trim is unavailable."""
        with (
            patch.object(main_module.sys, "platform", "linux"),
            patch.object(main_module.ctypes, "CDLL", side_effect=OSError("no libc")),
        ):
            main_module._trim_memory()  # must not raise


class TestPeriodicFullRescan:
    """RELIAB-01: a full rescan runs every FULL_RESCAN_INTERVAL_SECONDS as a safety net,
    independent of the real-time watcher — catches anything it might have silently missed
    (e.g. inotify event queue overflow from heavy background filesystem churn).

    _maybe_run_periodic_rescan() is tested directly (not via the full start() flow):
    patching time.monotonic() through start() also intercepts asyncio's own internal use of
    time.monotonic() for scheduling, making call counts unpredictable.
    """

    async def test_does_not_rescan_before_interval_elapsed(self, patched_app_explicit_creds):
        """With the interval not yet elapsed, scan_all_folders is not called again."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        app._last_full_scan_monotonic = 1000.0
        with patch.object(main_module.time, "monotonic", return_value=1000.0 + 60):
            await app._maybe_run_periodic_rescan()
        fl.scan_all_folders.assert_not_awaited()

    async def test_rescans_after_interval_elapsed(self, patched_app_explicit_creds):
        """Once FULL_RESCAN_INTERVAL_SECONDS has elapsed, a full rescan is triggered."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        base = 1000.0
        later = base + main_module.FULL_RESCAN_INTERVAL_SECONDS + 1
        app._last_full_scan_monotonic = base
        with patch.object(main_module.time, "monotonic", return_value=later):
            await app._maybe_run_periodic_rescan()
        fl.scan_all_folders.assert_awaited_once()

    async def test_last_full_scan_monotonic_updates_after_periodic_rescan(self, patched_app_explicit_creds):
        """_last_full_scan_monotonic is refreshed to the new timestamp after the periodic rescan."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        base = 1000.0
        later = base + main_module.FULL_RESCAN_INTERVAL_SECONDS + 1
        app._last_full_scan_monotonic = base
        with patch.object(main_module.time, "monotonic", return_value=later):
            await app._maybe_run_periodic_rescan()
        assert app._last_full_scan_monotonic == later

    async def test_start_sets_last_full_scan_monotonic_after_initial_scan(self, patched_app_explicit_creds):
        """AWSCopierApp.start() records _last_full_scan_monotonic right after the initial scan."""
        app, s3, fl, fw, cfg = patched_app_explicit_creds
        assert app._last_full_scan_monotonic == 0.0
        await app.start()
        assert app._last_full_scan_monotonic > 0.0


class TestConfigPathWiring:
    """Regression tests: AWSCopierApp must load the same config.yaml that main() gate-checks.

    Previously AWSCopierApp.__init__ called load_config() with no argument, which defaults
    to DEFAULT_CONFIG_PATH (~/aws-copier-config.yaml) — silently ignoring the local
    config.yaml that main() had just verified exists. This broke in any environment without
    a pre-existing ~/aws-copier-config.yaml (e.g. a Docker container with no home directory).
    """

    def test_default_config_path_is_local_config_yaml(self):
        """With no argument, AWSCopierApp loads config.yaml from the working directory."""
        cfg = _make_test_config(use_chain=False)
        with (
            patch.object(main_module, "load_config", return_value=cfg) as mock_load_config,
            patch.object(main_module, "S3Manager"),
            patch.object(main_module, "FileListener"),
            patch.object(main_module, "FolderWatcher"),
        ):
            AWSCopierApp()
            mock_load_config.assert_called_once_with(Path("config.yaml"))

    def test_explicit_config_path_is_passed_through(self):
        """A caller-supplied config_path is forwarded to load_config unchanged."""
        cfg = _make_test_config(use_chain=False)
        custom_path = Path("/tmp/aws-copier-test/custom-config.yaml")
        with (
            patch.object(main_module, "load_config", return_value=cfg) as mock_load_config,
            patch.object(main_module, "S3Manager"),
            patch.object(main_module, "FileListener"),
            patch.object(main_module, "FolderWatcher"),
        ):
            AWSCopierApp(config_path=custom_path)
            mock_load_config.assert_called_once_with(custom_path)
