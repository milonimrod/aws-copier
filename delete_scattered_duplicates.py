"""Standalone tool: delete the local + S3 copies of already-organized files sitting in a
לפזר staging folder (as identified by find_scattered_duplicates.py).

Safe by construction: a file is only deleted here when its content (by MD5) is confirmed
to already exist in a real, non-לפזר album elsewhere — the organized copy (and its S3
object) is never touched, only the redundant לפזר copy.

Needs S3 credentials (--config) since it also deletes the לפזר copy's now-orphaned S3
object — the daemon itself never does this on its own (see file_listener.py: no delete
logic exists for files that vanish locally).

Dry-run by default:
    docker exec aws-copier python delete_scattered_duplicates.py /data/pictures --config config.yaml
    docker exec aws-copier python delete_scattered_duplicates.py /data/pictures --config config.yaml --execute
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List

from find_scattered_duplicates import build_index, find_scattered_already_organized, print_report

logger = logging.getLogger(__name__)


def _remove_stale_backup_info(dirpath: Path) -> None:
    """Delete a directory's .milo_backup.info if it's the only thing left in it."""
    info_file = dirpath / ".milo_backup.info"
    if info_file.is_file() and list(dirpath.iterdir()) == [info_file]:
        info_file.unlink()


def _remove_empty_dirs(dirs: List[Path], floors: List[Path]) -> int:
    """Remove any of the given directories (and their now-possibly-empty ancestors) that
    are empty, bottom-up. Returns count removed.

    Only touches directories that actually held a deleted file — never walks the whole
    pictures tree — since the vast majority of the library is untouched by this cleanup.
    `floors` (the original scan roots) are never removed, even if they end up empty —
    a hard safety floor on how far the climb-up can go.
    """
    removed = 0
    # Longest paths (deepest) first, so a child empties out before its parent is checked.
    for dirpath in sorted(set(dirs), key=lambda p: len(p.parts), reverse=True):
        current = dirpath
        while current.is_dir() and current not in floors:
            _remove_stale_backup_info(current)
            try:
                current.rmdir()
                removed += 1
                current = current.parent
            except OSError:
                break  # not empty (or already gone) — stop climbing this branch
    return removed


async def _delete_s3_objects(stale: List[dict], config) -> int:
    from aws_copier.core.s3_manager import S3Manager

    s3_manager = S3Manager(config)
    await s3_manager.initialize()
    deleted = 0
    try:
        for entry in stale:
            s3_key = entry.get("s3_key")
            if not s3_key:
                logger.warning(f"No recorded S3 key for {entry['scattered_path']}, skipping S3 delete")
                continue
            if await s3_manager.delete_object(s3_key):
                deleted += 1
            else:
                logger.error(f"Failed to delete S3 object for {entry['scattered_path']}: {s3_key}")
    finally:
        await s3_manager.close()
    return deleted


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", type=Path, nargs="+", metavar="PATH", help="Folder(s) to scan for לפזר duplicates")
    parser.add_argument("--config", type=Path, required=True, help="config.yaml with S3 credentials")
    parser.add_argument("--execute", action="store_true", help="Actually delete (default: dry run)")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if not args.config.exists():
        sys.exit(f"Error: config not found at {args.config}")
    from aws_copier.models.simple_config import load_config

    config = load_config(args.config)

    print("AWS Copier — לפזר Duplicate Deleter")
    by_md5 = build_index(args.paths)
    stale = find_scattered_already_organized(by_md5)
    print_report(stale)

    if not stale:
        return

    if not args.execute:
        print("\nDry run only — nothing deleted. Pass --execute to actually delete these files.")
        return

    local_deleted = 0
    touched_dirs = []
    for entry in stale:
        try:
            entry["scattered_path"].unlink()
            local_deleted += 1
            touched_dirs.append(entry["scattered_path"].parent)
        except OSError as e:
            logger.error(f"Failed to delete local file {entry['scattered_path']}: {e}")

    dirs_removed = _remove_empty_dirs(touched_dirs, floors=args.paths)

    s3_deleted = await _delete_s3_objects(stale, config)

    print(f"\nDeleted {local_deleted}/{len(stale)} local files, {s3_deleted}/{len(stale)} S3 objects.")
    print(f"Removed {dirs_removed} now-empty directories.")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
