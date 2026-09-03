"""Tests for cleanup_migrated_s3 — Phase 2 of the cellphone-backup reorganization tool."""

from unittest.mock import AsyncMock

import pytest

from cleanup_migrated_s3 import _build_s3_key, process_manifest
from aws_copier.models.simple_config import SimpleConfig


def _duplicate_entry(old_key="Pictures/dup.jpg", done=False):
    return {"type": "duplicate", "md5": "m1", "old_local_path": "x", "new_local_path": "y", "old_s3_key": old_key, "done": done}


def _keep_entry(old_key="Pictures/old.jpg", new_key="Pictures/2024/05/new.jpg", done=False):
    return {
        "type": "keep",
        "md5": "m1",
        "old_local_path": "x",
        "new_local_path": "y",
        "old_s3_key": old_key,
        "new_s3_key": new_key,
        "done": done,
    }


class TestProcessManifestDuplicates:
    """Duplicate entries are always safe to delete immediately — no timing dependency."""

    @pytest.mark.asyncio
    async def test_duplicate_deleted_in_execute_mode(self):
        entries = [_duplicate_entry()]
        s3 = AsyncMock()
        s3.delete_object.return_value = True

        counts = await process_manifest(entries, s3, execute=True)

        s3.delete_object.assert_awaited_once_with("Pictures/dup.jpg")
        assert entries[0]["done"] is True
        assert counts["duplicates_deleted"] == 1

    @pytest.mark.asyncio
    async def test_duplicate_not_deleted_in_dry_run(self):
        entries = [_duplicate_entry()]
        s3 = AsyncMock()

        counts = await process_manifest(entries, s3, execute=False)

        s3.delete_object.assert_not_called()
        assert entries[0]["done"] is False
        assert counts["duplicates_deleted"] == 1  # still counted, just not applied

    @pytest.mark.asyncio
    async def test_already_done_duplicate_is_skipped(self):
        entries = [_duplicate_entry(done=True)]
        s3 = AsyncMock()

        counts = await process_manifest(entries, s3, execute=True)

        s3.delete_object.assert_not_called()
        assert counts["already_done"] == 1


class TestProcessManifestKeeps:
    """Kept-file old keys are only deleted once the new key is confirmed present."""

    @pytest.mark.asyncio
    async def test_old_key_deleted_when_new_key_confirmed(self):
        entries = [_keep_entry()]
        s3 = AsyncMock()
        s3.check_exists.return_value = True
        s3.delete_object.return_value = True

        counts = await process_manifest(entries, s3, execute=True)

        s3.check_exists.assert_awaited_once_with("Pictures/2024/05/new.jpg")
        s3.delete_object.assert_awaited_once_with("Pictures/old.jpg")
        assert entries[0]["done"] is True
        assert counts["kept_old_key_deleted"] == 1

    @pytest.mark.asyncio
    async def test_old_key_not_deleted_when_new_key_missing(self):
        """Regression: never delete the only copy of a kept file's content."""
        entries = [_keep_entry()]
        s3 = AsyncMock()
        s3.check_exists.return_value = False

        counts = await process_manifest(entries, s3, execute=True)

        s3.delete_object.assert_not_called()
        assert entries[0]["done"] is False
        assert counts["kept_waiting"] == 1

    @pytest.mark.asyncio
    async def test_missing_new_key_field_is_skipped_not_deleted(self):
        entries = [{"type": "keep", "old_s3_key": "Pictures/old.jpg", "done": False}]
        s3 = AsyncMock()

        counts = await process_manifest(entries, s3, execute=True)

        s3.delete_object.assert_not_called()
        assert counts["skipped_no_key"] == 1


class TestBuildS3Key:
    def test_matches_watch_folder_convention(self, tmp_path):
        config = SimpleConfig(watch_folders=[str(tmp_path)], s3_prefix="")
        local_path = tmp_path / "2024" / "05" / "photo.jpg"

        key = _build_s3_key(local_path, config)

        assert key == f"{tmp_path.name}/2024/05/photo.jpg"
