"""Tests for delete_scattered_duplicates — deletion of already-organized לפזר files."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from delete_scattered_duplicates import _delete_s3_objects, _remove_empty_dirs
from find_scattered_duplicates import build_index, find_scattered_already_organized


def _write_backup_info(folder: Path, files: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    entries = {}
    for filename, md5 in files.items():
        (folder / filename).write_bytes(b"content-" + md5.encode())
        entries[filename] = {"md5": md5, "mtime": 0.0, "s3_key": f"Pictures/{folder.name}/{filename}"}
    (folder / ".milo_backup.info").write_text(json.dumps({"files": entries}))


class TestRemoveEmptyDirs:
    def test_removes_now_empty_dir_after_file_deleted(self, tmp_path):
        scatter = tmp_path / "לפזר"
        scatter.mkdir()
        (scatter / "photo.jpg").write_bytes(b"x")
        (scatter / "photo.jpg").unlink()

        removed = _remove_empty_dirs([scatter], floors=[tmp_path])

        assert removed == 1
        assert not scatter.exists()

    def test_never_removes_the_scan_root_even_if_empty(self, tmp_path):
        scatter = tmp_path / "לפזר"
        scatter.mkdir()

        removed = _remove_empty_dirs([scatter], floors=[scatter])

        assert removed == 0
        assert scatter.exists()

    def test_does_not_remove_dir_with_remaining_files(self, tmp_path):
        scatter = tmp_path / "לפזר"
        scatter.mkdir()
        (scatter / "still_here.jpg").write_bytes(b"x")

        removed = _remove_empty_dirs([scatter], floors=[tmp_path])

        assert removed == 0
        assert scatter.exists()

    def test_climbs_up_through_multiple_empty_ancestors(self, tmp_path):
        nested = tmp_path / "old_computer" / "Pictures" / "לפזר"
        nested.mkdir(parents=True)

        removed = _remove_empty_dirs([nested], floors=[tmp_path])

        assert removed == 3
        assert not (tmp_path / "old_computer").exists()
        assert tmp_path.exists()

    def test_stale_backup_info_alone_does_not_block_removal(self, tmp_path):
        scatter = tmp_path / "לפזר"
        scatter.mkdir()
        (scatter / ".milo_backup.info").write_text('{"files": {}}')

        removed = _remove_empty_dirs([scatter], floors=[tmp_path])

        assert removed == 1
        assert not scatter.exists()


class TestDeleteS3Objects:
    @pytest.mark.asyncio
    async def test_deletes_each_stale_entrys_own_s3_key(self, monkeypatch):
        stale = [
            {"scattered_path": Path("/x/a.jpg"), "organized_paths": [Path("/y/a.jpg")], "size": 1, "s3_key": "k/a.jpg"},
            {"scattered_path": Path("/x/b.jpg"), "organized_paths": [Path("/y/b.jpg")], "size": 1, "s3_key": "k/b.jpg"},
        ]
        mock_s3 = AsyncMock()
        mock_s3.delete_object.return_value = True

        import aws_copier.core.s3_manager as s3_mod

        monkeypatch.setattr(s3_mod, "S3Manager", lambda config: mock_s3)
        deleted = await _delete_s3_objects(stale, config=object())

        assert deleted == 2
        mock_s3.delete_object.assert_any_await("k/a.jpg")
        mock_s3.delete_object.assert_any_await("k/b.jpg")

    @pytest.mark.asyncio
    async def test_missing_s3_key_is_skipped_not_crashed(self, monkeypatch):
        stale = [{"scattered_path": Path("/x/a.jpg"), "organized_paths": [], "size": 1, "s3_key": None}]
        mock_s3 = AsyncMock()

        import aws_copier.core.s3_manager as s3_mod

        monkeypatch.setattr(s3_mod, "S3Manager", lambda config: mock_s3)
        deleted = await _delete_s3_objects(stale, config=object())

        assert deleted == 0
        mock_s3.delete_object.assert_not_called()


class TestEndToEndSafety:
    def test_organized_copy_is_never_in_the_delete_list(self, tmp_path):
        """Regression: only the scattered path is ever a deletion candidate."""
        _write_backup_info(tmp_path / "לפזר", {"photo.jpg": "same_md5"})
        _write_backup_info(tmp_path / "אלבום אמיתי", {"photo_organized.jpg": "same_md5"})

        by_md5 = build_index([tmp_path])
        stale = find_scattered_already_organized(by_md5)

        organized_file = tmp_path / "אלבום אמיתי" / "photo_organized.jpg"
        assert all(entry["scattered_path"] != organized_file for entry in stale)
        assert organized_file.exists()
