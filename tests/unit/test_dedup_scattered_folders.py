"""Tests for dedup_scattered_folders — dedup WITHIN/ACROSS לפזר folders themselves."""

import json
from pathlib import Path

from dedup_scattered_folders import find_duplicate_groups_within_scatter
from find_scattered_duplicates import build_index


def _write_backup_info(folder: Path, files: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    entries = {}
    for filename, md5 in files.items():
        (folder / filename).write_bytes(b"content-" + md5.encode())
        entries[filename] = {"md5": md5, "mtime": 0.0, "s3_key": f"Pictures/{folder.name}/{filename}"}
    (folder / ".milo_backup.info").write_text(json.dumps({"files": entries}))


class TestFindDuplicateGroupsWithinScatter:
    def test_same_file_in_two_scatter_folders_is_flagged(self, tmp_path):
        _write_backup_info(tmp_path / "לפזר", {"a.jpg": "same_md5"})
        _write_backup_info(tmp_path / "old_computer" / "לפזר", {"b.jpg": "same_md5"})

        by_md5 = build_index([tmp_path])
        groups = find_duplicate_groups_within_scatter(by_md5)

        assert len(groups) == 1
        assert len(groups[0]["duplicates"]) == 1

    def test_duplicate_within_a_single_scatter_folder_is_flagged(self, tmp_path):
        _write_backup_info(tmp_path / "לפזר", {"a.jpg": "same_md5", "b.jpg": "same_md5"})

        by_md5 = build_index([tmp_path])
        groups = find_duplicate_groups_within_scatter(by_md5)

        assert len(groups) == 1
        assert len(groups[0]["duplicates"]) == 1

    def test_single_scattered_copy_is_not_flagged(self, tmp_path):
        _write_backup_info(tmp_path / "לפזר", {"a.jpg": "unique_md5"})

        by_md5 = build_index([tmp_path])
        groups = find_duplicate_groups_within_scatter(by_md5)

        assert groups == []

    def test_when_an_organized_copy_also_exists_it_is_not_flagged_here(self, tmp_path):
        """That's find_scattered_duplicates.py's job — no overlap between the two tools."""
        _write_backup_info(tmp_path / "לפזר", {"a.jpg": "same_md5"})
        _write_backup_info(tmp_path / "old_computer" / "לפזר", {"b.jpg": "same_md5"})
        _write_backup_info(tmp_path / "אלבום אמיתי", {"c.jpg": "same_md5"})

        by_md5 = build_index([tmp_path])
        groups = find_duplicate_groups_within_scatter(by_md5)

        assert groups == []

    def test_keeper_is_deterministic_alphabetically_first_path(self, tmp_path):
        _write_backup_info(tmp_path / "z_parent" / "לפזר", {"a.jpg": "same_md5"})
        _write_backup_info(tmp_path / "a_parent" / "לפזר", {"b.jpg": "same_md5"})

        by_md5 = build_index([tmp_path])
        groups = find_duplicate_groups_within_scatter(by_md5)

        assert groups[0]["keeper"] == tmp_path / "a_parent" / "לפזר" / "b.jpg"
        assert groups[0]["duplicates"] == [tmp_path / "z_parent" / "לפזר" / "a.jpg"]

    def test_three_way_duplicate_keeps_exactly_one(self, tmp_path):
        _write_backup_info(tmp_path / "parent1" / "לפזר", {"a.jpg": "same_md5"})
        _write_backup_info(tmp_path / "parent2" / "לפזר", {"b.jpg": "same_md5"})
        _write_backup_info(tmp_path / "parent3" / "לפזר", {"c.jpg": "same_md5"})

        by_md5 = build_index([tmp_path])
        groups = find_duplicate_groups_within_scatter(by_md5)

        assert len(groups) == 1
        assert len(groups[0]["duplicates"]) == 2
