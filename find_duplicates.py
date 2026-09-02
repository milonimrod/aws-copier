"""Standalone tool: find duplicate files and folders across watched libraries by reusing
MD5 checksums already recorded in .milo_backup.info — no re-hashing needed.

Must be run somewhere the config's watch_folders paths actually resolve. Since
config.yaml's watch_folders are container paths (e.g. /data/pictures), the normal way to
run this is inside the running container:

    docker exec aws-copier python find_duplicates.py

Running it locally instead needs a config.yaml whose watch_folders match paths this
machine can actually see.
"""

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from aws_copier.models.simple_config import SimpleConfig, load_config

logger = logging.getLogger(__name__)


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} PB"


def _iter_backup_info_files(watch_folders: List[Path]):
    """Yield every .milo_backup.info file under any of the given watch folders."""
    for root in watch_folders:
        if not root.is_dir():
            logger.warning(f"Watch folder does not exist, skipping: {root}")
            continue
        yield from root.rglob(".milo_backup.info")


def build_index(db_path: Path, watch_folders: List[Path]) -> sqlite3.Connection:
    """Populate a SQLite index of every tracked file's md5/size/path from .milo_backup.info.

    Reuses MD5s already computed by the daemon during normal scans — this never re-hashes
    a single file locally.

    Args:
        db_path: Where to create the SQLite file.
        watch_folders: Root folders to search for .milo_backup.info files under.

    Returns:
        Open sqlite3.Connection to the populated database.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE files (
            md5 TEXT NOT NULL,
            local_path TEXT NOT NULL,
            folder TEXT NOT NULL,
            filename TEXT NOT NULL,
            size INTEGER,
            s3_key TEXT
        )
        """
    )
    conn.execute("CREATE INDEX idx_files_md5 ON files(md5)")
    conn.execute("CREATE INDEX idx_files_folder ON files(folder)")

    rows = []
    backup_info_count = 0
    for info_file in _iter_backup_info_files(watch_folders):
        backup_info_count += 1
        try:
            data = json.loads(info_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not read {info_file}: {e}")
            continue

        folder = info_file.parent
        for filename, entry in data.get("files", {}).items():
            md5 = entry.get("md5") if isinstance(entry, dict) else entry
            s3_key = entry.get("s3_key") if isinstance(entry, dict) else None
            if not md5:
                continue
            local_path = folder / filename
            try:
                size: Optional[int] = local_path.stat().st_size
            except OSError:
                # Entry is stale (file since deleted/moved locally) — still index it by
                # md5/path for reference, just without a size for the wasted-space total.
                size = None
            rows.append((md5, str(local_path), str(folder), filename, size, s3_key))

    conn.executemany(
        "INSERT INTO files (md5, local_path, folder, filename, size, s3_key) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    logger.info(f"Indexed {len(rows)} file records from {backup_info_count} .milo_backup.info files")
    return conn


def report_duplicate_files(conn: sqlite3.Connection, min_size: int = 0) -> int:
    """Print duplicate-file groups (same md5, multiple paths), largest wasted space first.

    Args:
        conn: Open connection to a database built by build_index().
        min_size: Skip groups whose file size is below this (bytes).

    Returns:
        Total wasted bytes across all reported duplicate groups (N-1 copies per group).
    """
    groups = conn.execute(
        """
        SELECT md5, COUNT(*) as cnt, MAX(size) as size
        FROM files
        WHERE size IS NOT NULL AND size >= ?
        GROUP BY md5
        HAVING COUNT(*) > 1
        ORDER BY (cnt - 1) * size DESC
        """,
        (min_size,),
    ).fetchall()

    if not groups:
        print("\nNo duplicate files found.")
        return 0

    total_wasted = 0
    print(f"\n=== Duplicate files ({len(groups)} groups) ===\n")
    for md5, cnt, size in groups:
        wasted = (cnt - 1) * size
        total_wasted += wasted
        paths = conn.execute(
            "SELECT local_path FROM files WHERE md5 = ? ORDER BY local_path", (md5,)
        ).fetchall()
        print(f"{cnt}x  {_format_bytes(size)} each  (wastes {_format_bytes(wasted)})  md5={md5}")
        for (path,) in paths:
            print(f"    {path}")
        print()

    print(f"Total wasted space from duplicate files: {_format_bytes(total_wasted)}")
    return total_wasted


def report_duplicate_folders(conn: sqlite3.Connection) -> int:
    """Print folders whose full (filename, md5) content set exactly matches another folder's.

    Args:
        conn: Open connection to a database built by build_index().

    Returns:
        Number of duplicate-folder groups found.
    """
    folders = [row[0] for row in conn.execute("SELECT DISTINCT folder FROM files").fetchall()]

    signatures: Dict[str, List[str]] = {}
    for folder in folders:
        rows = conn.execute(
            "SELECT filename, md5 FROM files WHERE folder = ? ORDER BY filename", (folder,)
        ).fetchall()
        if not rows:
            continue
        fingerprint_input = "\n".join(f"{name}:{md5}" for name, md5 in rows)
        signature = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
        signatures.setdefault(signature, []).append(folder)

    dup_groups = [group for group in signatures.values() if len(group) > 1]
    if not dup_groups:
        print("\nNo duplicate folders found.")
        return 0

    print(f"\n=== Duplicate folders ({len(dup_groups)} groups — identical filenames AND content) ===\n")
    for group in dup_groups:
        print(f"{len(group)}x identical folders:")
        for folder in sorted(group):
            print(f"    {folder}")
        print()

    return len(dup_groups)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find duplicate files/folders across watched libraries using MD5 "
        "checksums already recorded in .milo_backup.info (no re-hashing).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  docker exec aws-copier python find_duplicates.py
  docker exec aws-copier python find_duplicates.py --min-size 1048576
  uv run python find_duplicates.py --config ~/my-config.yaml --keep-db
""",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Config YAML path (default: config.yaml in the working directory — matches "
        "where the daemon expects it, e.g. /app/config.yaml inside the container; NOT "
        "DEFAULT_CONFIG_PATH/~/aws-copier-config.yaml, which `docker exec` — running as "
        "root, $HOME=/root — would silently miss)",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=0,
        metavar="BYTES",
        help="Ignore duplicate files smaller than this (default: 0, no filter)",
    )
    parser.add_argument(
        "--folders-only",
        action="store_true",
        help="Skip the file-level report, only check for duplicate folders",
    )
    parser.add_argument(
        "--files-only",
        action="store_true",
        help="Skip the folder-level report, only check for duplicate files",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Keep the SQLite index file after the report instead of deleting it",
    )
    parser.add_argument("--verbose", action="store_true", help="Show debug logging")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    config_path = args.config or Path("config.yaml")
    # NOTE: load_config() never raises FileNotFoundError for a missing path — it silently
    # creates a template config there instead (with bogus default watch_folders) and
    # returns that. Must check existence explicitly first, or a missing/wrong config_path
    # silently "succeeds" against the wrong (empty) library instead of erroring out.
    if not config_path.exists():
        sys.exit(f"Error: config not found at {config_path}")
    config: SimpleConfig = load_config(config_path)

    print("AWS Copier — Duplicate Finder")
    print(f"Watch folders: {', '.join(str(f) for f in config.watch_folders)}")

    db_fd, db_path_str = tempfile.mkstemp(suffix=".sqlite", prefix="aws-copier-dupes-")
    os.close(db_fd)
    db_path = Path(db_path_str)

    try:
        conn = build_index(db_path, config.watch_folders)
        try:
            if not args.folders_only:
                report_duplicate_files(conn, args.min_size)
            if not args.files_only:
                report_duplicate_folders(conn)
        finally:
            conn.close()
    finally:
        if args.keep_db:
            print(f"\nSQLite index kept at: {db_path}")
        else:
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
