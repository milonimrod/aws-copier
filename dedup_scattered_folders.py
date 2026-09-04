"""Standalone tool: deduplicate files WITHIN/ACROSS לפזר staging folders themselves.

This is the other duplicate case, distinct from find_scattered_duplicates.py: that tool
only flags a לפזר file whose content is *already organized* into a real album elsewhere.
This one handles files copied into לפזר more than once with no organized copy existing yet
at all (e.g. the same camera-import duplicated across both לפזר locations, or duplicated
within a single one) — deliberately excluded from find_scattered_duplicates.py's report.

Keeps exactly one copy per duplicate group (deterministic: the alphabetically-first path)
and deletes the local file for every other copy. Never touches S3 directly — the watcher's
own deletion-propagation feature (DEL-01) picks up the S3-side cleanup automatically on its
next periodic scan (soft-delete to _trash/, capped by config.yaml's max_deletions_per_scan
per folder), so this script needs no S3 credentials at all.

Dry-run by default:
    docker exec aws-copier python dedup_scattered_folders.py /data/pictures
    docker exec aws-copier python dedup_scattered_folders.py /data/pictures --execute
"""

import argparse
import logging
from pathlib import Path
from typing import Dict, List

from delete_scattered_duplicates import _remove_empty_dirs
from find_scattered_duplicates import _format_bytes, build_index

logger = logging.getLogger(__name__)


def find_duplicate_groups_within_scatter(by_md5: Dict[str, List[dict]]) -> List[dict]:
    """Find MD5 groups where every copy sits in a לפזר folder — no organized copy exists.

    Args:
        by_md5: Index built by find_scattered_duplicates.build_index().

    Returns:
        List of {"keeper": Path, "duplicates": [Path, ...], "dup_size_total": int}, one per
        MD5 group with 2+ scattered-only copies, sorted by wasted size descending.
    """
    groups = []
    for entries in by_md5.values():
        scattered = [e for e in entries if e["scattered"]]
        organized = [e for e in entries if not e["scattered"]]
        if organized or len(scattered) < 2:
            continue  # an organized copy exists (find_scattered_duplicates.py's case), or no duplicate at all
        scattered_sorted = sorted(scattered, key=lambda e: str(e["path"]))
        keeper, duplicates = scattered_sorted[0], scattered_sorted[1:]
        groups.append(
            {
                "keeper": keeper["path"],
                "duplicates": [d["path"] for d in duplicates],
                "dup_size_total": sum(d["size"] or 0 for d in duplicates),
            }
        )
    groups.sort(key=lambda g: g["dup_size_total"], reverse=True)
    return groups


def print_report(groups: List[dict]) -> int:
    """Print the report. Returns total wasted bytes."""
    if not groups:
        print("\nNo duplicate files found within לפזר folders.")
        return 0

    total = sum(g["dup_size_total"] for g in groups)
    print(f"\n=== Duplicate files within לפזר folders ({len(groups)} groups) ===\n")
    for g in groups:
        print(f"  keep: {g['keeper']}")
        for d in g["duplicates"]:
            print(f"    dup: {d}")
    print(f"\nTotal reclaimable: {_format_bytes(total)}")
    return total


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", type=Path, nargs="+", metavar="PATH", help="Folder(s) to scan")
    parser.add_argument("--execute", action="store_true", help="Actually delete duplicates locally (default: dry run)")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    print("AWS Copier — לפזר Internal Duplicate Finder")
    by_md5 = build_index(args.paths)
    groups = find_duplicate_groups_within_scatter(by_md5)
    print_report(groups)

    if not groups:
        return

    if not args.execute:
        print("\nDry run only — nothing deleted. Pass --execute to delete duplicates locally.")
        print("S3 cleanup happens automatically via the watcher's next scan — no S3 credentials needed here.")
        return

    touched_dirs = []
    deleted = 0
    for g in groups:
        for dup_path in g["duplicates"]:
            try:
                dup_path.unlink()
                deleted += 1
                touched_dirs.append(dup_path.parent)
            except OSError as e:
                logger.error(f"Failed to delete {dup_path}: {e}")

    dirs_removed = _remove_empty_dirs(touched_dirs, floors=args.paths)
    print(f"\nDeleted {deleted} local duplicate files. Removed {dirs_removed} now-empty directories.")
    print("S3 cleanup will happen automatically on the daemon's next periodic scan (capped per-folder — see max_deletions_per_scan in config.yaml).")


if __name__ == "__main__":
    main()
