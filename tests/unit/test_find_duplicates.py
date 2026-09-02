"""Tests for the find_duplicates standalone duplicate-detection tool."""

import json
from pathlib import Path

import pytest

import find_duplicates
from find_duplicates import build_index, report_duplicate_files, report_duplicate_folders


def _write_backup_info(folder: Path, files: dict) -> None:
    """Write a .milo_backup.info in `folder` for the given {filename: md5} mapping, and
    create each named file on disk (needed since build_index() stats the local file for size).
    """
    folder.mkdir(parents=True, exist_ok=True)
    entries = {}
    for filename, md5 in files.items():
        content = f"content-for-{md5}"
        (folder / filename).write_text(content)
        entries[filename] = {
            "md5": md5,
            "mtime": 0.0,
            "s3_key": f"Pictures/{folder.name}/{filename}",
        }
    (folder / ".milo_backup.info").write_text(json.dumps({"files": entries}))


class TestBuildIndex:
    """build_index() reuses recorded MD5s from .milo_backup.info without re-hashing."""

    def test_indexes_files_from_multiple_backup_info_files(self, tmp_path):
        _write_backup_info(tmp_path / "album_a", {"photo1.jpg": "aaa111"})
        _write_backup_info(tmp_path / "album_b", {"photo2.jpg": "bbb222"})

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        rows = conn.execute("SELECT md5, filename FROM files ORDER BY filename").fetchall()
        conn.close()

        assert rows == [("aaa111", "photo1.jpg"), ("bbb222", "photo2.jpg")]

    def test_records_file_size_from_disk(self, tmp_path):
        _write_backup_info(tmp_path / "album", {"photo.jpg": "aaa111"})
        expected_size = (tmp_path / "album" / "photo.jpg").stat().st_size

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        (size,) = conn.execute("SELECT size FROM files").fetchone()
        conn.close()

        assert size == expected_size

    def test_stale_entry_missing_local_file_gets_null_size_not_crash(self, tmp_path):
        """A .milo_backup.info entry for a file since deleted locally must not crash indexing."""
        folder = tmp_path / "album"
        folder.mkdir()
        entries = {"gone.jpg": {"md5": "aaa111", "mtime": 0.0, "s3_key": "Pictures/album/gone.jpg"}}
        (folder / ".milo_backup.info").write_text(json.dumps({"files": entries}))
        # Note: gone.jpg is never actually created on disk.

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        row = conn.execute("SELECT md5, size FROM files").fetchone()
        conn.close()

        assert row == ("aaa111", None)

    def test_malformed_backup_info_is_skipped_not_fatal(self, tmp_path):
        folder = tmp_path / "album"
        folder.mkdir()
        (folder / ".milo_backup.info").write_text("not valid json{{{")

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()

        assert count == 0

    def test_nonexistent_watch_folder_is_skipped_not_fatal(self, tmp_path):
        conn = build_index(tmp_path / "index.sqlite", [tmp_path / "does_not_exist"])
        count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()

        assert count == 0


class TestReportDuplicateFiles:
    """report_duplicate_files() groups by md5 and reports wasted space."""

    def test_detects_duplicate_across_folders(self, tmp_path, capsys):
        _write_backup_info(tmp_path / "album_a", {"photo.jpg": "same_md5"})
        _write_backup_info(tmp_path / "album_b", {"copy_of_photo.jpg": "same_md5"})

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        wasted = report_duplicate_files(conn)
        conn.close()

        output = capsys.readouterr().out
        assert "Duplicate files (1 groups)" in output
        assert "album_a" in output and "album_b" in output
        assert wasted > 0

    def test_no_duplicates_reports_zero(self, tmp_path, capsys):
        _write_backup_info(tmp_path / "album_a", {"photo1.jpg": "md5_one"})
        _write_backup_info(tmp_path / "album_b", {"photo2.jpg": "md5_two"})

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        wasted = report_duplicate_files(conn)
        conn.close()

        assert wasted == 0
        assert "No duplicate files found" in capsys.readouterr().out

    def test_min_size_filters_small_duplicates(self, tmp_path, capsys):
        _write_backup_info(tmp_path / "album_a", {"tiny.jpg": "same_md5"})
        _write_backup_info(tmp_path / "album_b", {"tiny_copy.jpg": "same_md5"})

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        # The fixture files are a handful of bytes — anything requiring 1MB+ excludes them.
        wasted = report_duplicate_files(conn, min_size=1024 * 1024)
        conn.close()

        assert wasted == 0
        assert "No duplicate files found" in capsys.readouterr().out

    def test_wasted_space_is_n_minus_one_copies(self, tmp_path, capsys):
        """3 identical files: wasted space = 2x the file size, not 3x."""
        _write_backup_info(
            tmp_path / "album",
            {"a.jpg": "same_md5", "b.jpg": "same_md5", "c.jpg": "same_md5"},
        )
        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        (size,) = conn.execute("SELECT size FROM files LIMIT 1").fetchone()
        wasted = report_duplicate_files(conn)
        conn.close()

        assert wasted == 2 * size


class TestReportDuplicateFolders:
    """report_duplicate_folders() flags folders with an identical (filename, md5) set."""

    def test_detects_identical_folders(self, tmp_path, capsys):
        contents = {"a.jpg": "md5_a", "b.jpg": "md5_b"}
        _write_backup_info(tmp_path / "album_original", contents)
        _write_backup_info(tmp_path / "album_backup_copy", contents)

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        count = report_duplicate_folders(conn)
        conn.close()

        assert count == 1
        output = capsys.readouterr().out
        assert "album_original" in output and "album_backup_copy" in output

    def test_folders_with_different_content_are_not_flagged(self, tmp_path, capsys):
        _write_backup_info(tmp_path / "album_a", {"a.jpg": "md5_a"})
        _write_backup_info(tmp_path / "album_b", {"a.jpg": "md5_different"})

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        count = report_duplicate_folders(conn)
        conn.close()

        assert count == 0
        assert "No duplicate folders found" in capsys.readouterr().out

    def test_folders_with_partial_overlap_are_not_flagged(self, tmp_path, capsys):
        """A folder that's a SUBSET of another's content is not an exact duplicate."""
        _write_backup_info(tmp_path / "album_full", {"a.jpg": "md5_a", "b.jpg": "md5_b"})
        _write_backup_info(tmp_path / "album_partial", {"a.jpg": "md5_a"})

        conn = build_index(tmp_path / "index.sqlite", [tmp_path])
        count = report_duplicate_folders(conn)
        conn.close()

        assert count == 0


class TestMainArgumentHandling:
    """main() takes plain folder paths and never requires config.yaml — the tool only
    reads .milo_backup.info files, it never touches S3 or credentials. --config is an
    optional convenience for reusing an existing config.yaml's watch_folders."""

    def test_no_paths_and_no_config_exits_with_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["find_duplicates.py"])

        with pytest.raises(SystemExit) as exc_info:
            find_duplicates.main()

        assert "pass at least one PATH" in str(exc_info.value)

    def test_paths_and_config_together_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.argv", ["find_duplicates.py", str(tmp_path), "--config", "config.yaml"])

        with pytest.raises(SystemExit) as exc_info:
            find_duplicates.main()

        assert "either PATH arguments or --config" in str(exc_info.value)

    def test_missing_config_exits_with_error(self, tmp_path, monkeypatch):
        """Regression: a missing --config must error out, not silently create a template
        with bogus watch_folders and proceed against the wrong (empty) library — the same
        bug class fixed earlier in main.py's AWSCopierApp."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["find_duplicates.py", "--config", "config.yaml"])

        with pytest.raises(SystemExit) as exc_info:
            find_duplicates.main()

        assert "config not found" in str(exc_info.value)
        # And critically: no template config.yaml was silently created as a side effect.
        assert not (tmp_path / "config.yaml").exists()

    def test_explicit_path_scans_without_any_config(self, tmp_path, monkeypatch, capsys):
        _write_backup_info(tmp_path / "album", {"photo.jpg": "aaa111"})
        monkeypatch.setattr("sys.argv", ["find_duplicates.py", str(tmp_path)])

        find_duplicates.main()

        assert "No duplicate files found" in capsys.readouterr().out
