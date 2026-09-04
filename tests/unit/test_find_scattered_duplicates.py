"""Tests for the find_scattered_duplicates standalone tool."""

import json
from pathlib import Path

from find_scattered_duplicates import build_index, find_scattered_already_organized


def _write_backup_info(folder: Path, files: dict) -> None:
    """Create each named file on disk plus a .milo_backup.info recording its md5/s3_key."""
    folder.mkdir(parents=True, exist_ok=True)
    entries = {}
    for filename, md5 in files.items():
        (folder / filename).write_bytes(b"content-" + md5.encode())
        entries[filename] = {"md5": md5, "mtime": 0.0, "s3_key": f"Pictures/{folder.name}/{filename}"}
    (folder / ".milo_backup.info").write_text(json.dumps({"files": entries}))


class TestBuildIndex:
    def test_flags_files_under_a_scatter_folder(self, tmp_path):
        _write_backup_info(tmp_path / "לפזר", {"photo.jpg": "md5a"})
        _write_backup_info(tmp_path / "אלבום אמיתי", {"photo2.jpg": "md5b"})

        by_md5 = build_index([tmp_path])

        assert by_md5["md5a"][0]["scattered"] is True
        assert by_md5["md5b"][0]["scattered"] is False

    def test_scatter_folder_nested_under_real_album_is_still_flagged(self, tmp_path):
        _write_backup_info(tmp_path / "משפחת קדם" / "לפזר", {"photo.jpg": "md5a"})

        by_md5 = build_index([tmp_path])

        assert by_md5["md5a"][0]["scattered"] is True

    def test_folder_merely_containing_the_substring_is_not_flagged(self, tmp_path):
        """Only an exact path-component match counts — not a folder whose name merely
        contains "לפזר" as a substring."""
        _write_backup_info(tmp_path / "לפזר החדש", {"photo.jpg": "md5a"})

        by_md5 = build_index([tmp_path])

        assert by_md5["md5a"][0]["scattered"] is False


class TestFindScatteredAlreadyOrganized:
    def test_scattered_file_matching_organized_file_is_flagged(self, tmp_path):
        _write_backup_info(tmp_path / "לפזר", {"photo.jpg": "same_md5"})
        _write_backup_info(tmp_path / "אלבום אמיתי", {"photo_organized.jpg": "same_md5"})

        by_md5 = build_index([tmp_path])
        stale = find_scattered_already_organized(by_md5)

        assert len(stale) == 1
        assert stale[0]["scattered_path"].name == "photo.jpg"
        assert stale[0]["organized_paths"][0].name == "photo_organized.jpg"

    def test_scattered_file_with_no_organized_match_is_not_flagged(self, tmp_path):
        """A file only in לפזר (not yet sorted anywhere) must NOT be flagged — it hasn't
        been organized yet, so it's not safe to remove."""
        _write_backup_info(tmp_path / "לפזר", {"photo.jpg": "unique_md5"})

        by_md5 = build_index([tmp_path])
        stale = find_scattered_already_organized(by_md5)

        assert stale == []

    def test_duplicate_within_two_scatter_folders_is_not_flagged(self, tmp_path):
        """Two לפזר copies of the same file, with no real-album copy, isn't 'already
        organized' — both are still just staging clutter."""
        _write_backup_info(tmp_path / "לפזר", {"a.jpg": "same_md5"})
        _write_backup_info(tmp_path / "אחר" / "לפזר", {"b.jpg": "same_md5"})

        by_md5 = build_index([tmp_path])
        stale = find_scattered_already_organized(by_md5)

        assert stale == []

    def test_organized_file_itself_is_not_flagged(self, tmp_path):
        """Only the scattered copy is reported — never the real, already-organized one."""
        _write_backup_info(tmp_path / "לפזר", {"photo.jpg": "same_md5"})
        _write_backup_info(tmp_path / "אלבום אמיתי", {"photo_organized.jpg": "same_md5"})

        by_md5 = build_index([tmp_path])
        stale = find_scattered_already_organized(by_md5)

        flagged_names = {s["scattered_path"].name for s in stale}
        assert "photo_organized.jpg" not in flagged_names

    def test_sorted_by_size_descending(self, tmp_path):
        big_folder = tmp_path / "לפזר"
        big_folder.mkdir(parents=True)
        (big_folder / "big.jpg").write_bytes(b"x" * 1000)
        (big_folder / "small.jpg").write_bytes(b"y" * 10)
        entries = {
            "big.jpg": {"md5": "md5_big", "mtime": 0.0, "s3_key": "k/big.jpg"},
            "small.jpg": {"md5": "md5_small", "mtime": 0.0, "s3_key": "k/small.jpg"},
        }
        (big_folder / ".milo_backup.info").write_text(json.dumps({"files": entries}))
        _write_backup_info(tmp_path / "אלבום", {"big_copy.jpg": "md5_big", "small_copy.jpg": "md5_small"})

        by_md5 = build_index([tmp_path])
        stale = find_scattered_already_organized(by_md5)

        assert [s["scattered_path"].name for s in stale] == ["big.jpg", "small.jpg"]
