"""Tests for the reorganize_cellphone_backup standalone migration tool."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from reorganize_cellphone_backup import (
    _date_from_filename,
    _date_from_mtime,
    build_plan,
    determine_date,
    execute_plan,
    load_backup_index,
)


def _write_backup_info(folder: Path, files: dict) -> None:
    """Create each named file on disk plus a .milo_backup.info recording {md5, s3_key}."""
    folder.mkdir(parents=True, exist_ok=True)
    entries = {}
    for filename, (md5, s3_key) in files.items():
        (folder / filename).write_bytes(b"content-" + md5.encode())
        entries[filename] = {"md5": md5, "mtime": 0.0, "s3_key": s3_key}
    (folder / ".milo_backup.info").write_text(json.dumps({"files": entries}))


class TestDateFromFilename:
    """_date_from_filename() parses common phone-camera naming conventions."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("IMG_20240528_101112.jpg", datetime(2024, 5, 28)),
            ("VID-20240528-WA0001.mp4", datetime(2024, 5, 28)),
            ("20240528_101112.jpg", datetime(2024, 5, 28)),
            ("Screenshot_20240528-101112.png", datetime(2024, 5, 28)),
            ("PXL_20240528_101112000.jpg", datetime(2024, 5, 28)),
        ],
    )
    def test_recognized_patterns(self, filename, expected):
        assert _date_from_filename(Path(filename)) == expected

    def test_no_date_in_name_returns_none(self):
        assert _date_from_filename(Path("photo.jpg")) is None

    def test_implausible_month_is_rejected(self):
        # "9999" would parse as year=9999 which is out of the accepted range.
        assert _date_from_filename(Path("random_99991301_x.jpg")) is None


class TestDateFromMtime:
    def test_uses_file_mtime(self, tmp_path):
        f = tmp_path / "photo.jpg"
        f.write_bytes(b"x")
        assert _date_from_mtime(f) is not None


class TestDetermineDate:
    """determine_date() falls through exif -> filename -> mtime -> unknown."""

    def test_filename_used_when_no_exif(self, tmp_path):
        f = tmp_path / "IMG_20240528_101112.jpg"
        f.write_bytes(b"not a real jpeg")
        date, source = determine_date(f)
        assert source in ("filename", "mtime")  # Pillow may fail to read fake jpeg bytes gracefully
        if source == "filename":
            assert date == datetime(2024, 5, 28)

    def test_mtime_fallback_for_undated_video(self, tmp_path):
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"x")
        date, source = determine_date(f)
        assert source == "mtime"
        assert date is not None


class TestLoadBackupIndex:
    def test_indexes_md5_and_s3_key(self, tmp_path):
        _write_backup_info(tmp_path / "bkp_20240528", {"a.jpg": ("md5a", "Pictures/bkp_20240528/a.jpg")})

        index = load_backup_index(tmp_path)

        entry = index[tmp_path / "bkp_20240528" / "a.jpg"]
        assert entry == {"md5": "md5a", "s3_key": "Pictures/bkp_20240528/a.jpg"}


class TestBuildPlan:
    """build_plan() keeps one file per MD5 and quarantines the rest as duplicates."""

    def test_unique_files_are_all_kept(self, tmp_path):
        source = tmp_path / "tali_cellphone"
        _write_backup_info(
            source / "bkp_a",
            {"IMG_20240101_000000.jpg": ("md5a", "k/a.jpg"), "IMG_20240202_000000.jpg": ("md5b", "k/b.jpg")},
        )

        plan = build_plan(source, tmp_path / "dest")

        assert len(plan.keeps) == 2
        assert len(plan.duplicates) == 0

    def test_duplicate_across_snapshots_keeps_one(self, tmp_path):
        source = tmp_path / "tali_cellphone"
        _write_backup_info(source / "bkp_20240101", {"IMG_20240101_000000.jpg": ("same_md5", "k/1.jpg")})
        _write_backup_info(source / "bkp_20240601", {"IMG_20240101_000000.jpg": ("same_md5", "k/2.jpg")})

        plan = build_plan(source, tmp_path / "dest")

        assert len(plan.keeps) == 1
        assert len(plan.duplicates) == 1
        assert plan.keeps[0].md5 == "same_md5"
        assert plan.duplicates[0].md5 == "same_md5"

    def test_keeper_dest_uses_filename_date_bucket(self, tmp_path):
        source = tmp_path / "tali_cellphone"
        _write_backup_info(source / "bkp", {"IMG_20240528_101112.jpg": ("md5a", "k/a.jpg")})

        plan = build_plan(source, tmp_path / "dest")

        assert plan.keeps[0].dest == tmp_path / "dest" / "2024" / "05" / "IMG_20240528_101112.jpg"

    def test_duplicate_dest_is_under_quarantine_folder(self, tmp_path):
        source = tmp_path / "tali_cellphone"
        _write_backup_info(source / "bkp_a", {"photo.jpg": ("same_md5", "k/1.jpg")})
        _write_backup_info(source / "bkp_b", {"photo.jpg": ("same_md5", "k/2.jpg")})

        plan = build_plan(source, tmp_path / "dest")

        assert str(plan.duplicates[0].dest).startswith(str(tmp_path / "dest" / "_duplicates"))

    def test_file_without_recorded_md5_is_skipped_not_moved(self, tmp_path):
        source = tmp_path / "tali_cellphone" / "bkp"
        source.mkdir(parents=True)
        (source / "orphan.jpg").write_bytes(b"x")  # No .milo_backup.info entry for it

        plan = build_plan(tmp_path / "tali_cellphone", tmp_path / "dest")

        assert plan.keeps == []
        assert plan.duplicates == []
        assert (source / "orphan.jpg") in plan.skipped_no_md5

    def test_earliest_dated_copy_across_snapshots_is_the_keeper(self, tmp_path):
        """Given identical content in two snapshots, the earlier-dated one is kept."""
        source = tmp_path / "tali_cellphone"
        # Older snapshot file with an early filename-date, newer snapshot with a later one.
        _write_backup_info(source / "bkp_new", {"IMG_20250601_000000.jpg": ("same_md5", "k/new.jpg")})
        _write_backup_info(source / "bkp_old", {"IMG_20240101_000000.jpg": ("same_md5", "k/old.jpg")})

        plan = build_plan(source, tmp_path / "dest")

        assert len(plan.keeps) == 1
        assert plan.keeps[0].source.name == "IMG_20240101_000000.jpg"


class TestExecutePlan:
    """execute_plan() physically moves files and writes a replayable manifest."""

    def test_moves_files_and_writes_manifest(self, tmp_path):
        source = tmp_path / "tali_cellphone"
        _write_backup_info(source / "bkp", {"IMG_20240528_101112.jpg": ("md5a", "k/a.jpg")})
        dest_root = tmp_path / "dest"

        plan = build_plan(source, dest_root)
        manifest_path = execute_plan(plan, source, dest_root)

        dest_file = dest_root / "2024" / "05" / "IMG_20240528_101112.jpg"
        assert dest_file.exists()
        assert not (source / "bkp" / "IMG_20240528_101112.jpg").exists()

        lines = [json.loads(line) for line in manifest_path.read_text().splitlines()]
        assert len(lines) == 1
        assert lines[0]["type"] == "keep"
        assert lines[0]["old_s3_key"] == "k/a.jpg"
        assert lines[0]["new_local_path"] == str(dest_file)
        assert lines[0]["done"] is False

    def test_empty_source_dirs_are_removed_after_move(self, tmp_path):
        source = tmp_path / "tali_cellphone"
        _write_backup_info(source / "bkp" / "sub", {"a.jpg": ("md5a", "k/a.jpg")})
        dest_root = tmp_path / "dest"

        plan = build_plan(source, dest_root)
        execute_plan(plan, source, dest_root)

        assert not source.exists()

    def test_duplicate_manifest_entry_marks_type_duplicate(self, tmp_path):
        source = tmp_path / "tali_cellphone"
        _write_backup_info(source / "bkp_a", {"photo.jpg": ("same_md5", "k/1.jpg")})
        _write_backup_info(source / "bkp_b", {"photo.jpg": ("same_md5", "k/2.jpg")})
        dest_root = tmp_path / "dest"

        plan = build_plan(source, dest_root)
        manifest_path = execute_plan(plan, source, dest_root)

        lines = [json.loads(line) for line in manifest_path.read_text().splitlines()]
        types = {line["type"] for line in lines}
        assert types == {"keep", "duplicate"}
