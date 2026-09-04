"""Standalone tool: find files sitting in a "לפזר" (scatter/staging) folder that have
already been organized into a real album elsewhere — i.e. their content (by MD5) also
exists somewhere outside any לפזר folder.

לפזר folders are temporary staging areas — files get dropped there, then manually sorted
into real album folders later. A file still sitting in לפזר whose content already exists
in a non-לפזר folder is stale: it was already organized, and the לפזר copy is just leftover
clutter safe to remove (see cleanup_migrated_s3.py for the actual S3-side cleanup pattern,
since deleting the local file does NOT clean up S3 on its own — the daemon has no delete
logic at all).

Reuses MD5s already recorded in .milo_backup.info — no re-hashing.

    docker exec aws-copier python find_scattered_duplicates.py /data/pictures
"""

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SCATTER_FOLDER_NAME = "לפזר"


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} PB"


def _is_under_scatter_folder(path: Path, root: Path) -> bool:
    """True if any path component between root and this file is named לפזר."""
    return SCATTER_FOLDER_NAME in path.relative_to(root).parts[:-1]


def build_index(roots: List[Path]) -> Dict[str, List[dict]]:
    """Index every tracked file by MD5, reusing .milo_backup.info — no re-hashing.

    Args:
        roots: Watch-folder roots to search under.

    Returns:
        Mapping of md5 -> list of {"path": Path, "size": Optional[int], "s3_key": Optional[str],
        "scattered": bool} for every file recorded in any .milo_backup.info under those roots.
    """
    by_md5: Dict[str, List[dict]] = defaultdict(list)
    for root in roots:
        if not root.is_dir():
            logger.warning(f"Root does not exist, skipping: {root}")
            continue
        for info_file in root.rglob(".milo_backup.info"):
            try:
                data = json.loads(info_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Could not read {info_file}: {e}")
                continue
            folder = info_file.parent
            for filename, entry in data.get("files", {}).items():
                if isinstance(entry, dict):
                    md5 = entry.get("md5")
                    s3_key = entry.get("s3_key")
                else:
                    md5, s3_key = entry, None
                if not md5:
                    continue
                local_path = folder / filename
                try:
                    size: Optional[int] = local_path.stat().st_size
                except OSError:
                    size = None
                by_md5[md5].append(
                    {
                        "path": local_path,
                        "size": size,
                        "s3_key": s3_key,
                        "scattered": _is_under_scatter_folder(local_path, root),
                    }
                )
    return by_md5


def find_scattered_already_organized(by_md5: Dict[str, List[dict]]) -> List[dict]:
    """Find every scattered (לפזר) file whose content also exists outside any לפזר folder.

    Args:
        by_md5: Index built by build_index().

    Returns:
        List of {"scattered_path", "organized_paths", "size", "s3_key"} — one entry per
        stale לפזר file, sorted by size descending.
    """
    stale = []
    for md5, entries in by_md5.items():
        scattered = [e for e in entries if e["scattered"]]
        organized = [e for e in entries if not e["scattered"]]
        if not scattered or not organized:
            continue
        for s in scattered:
            stale.append(
                {
                    "scattered_path": s["path"],
                    "organized_paths": [o["path"] for o in organized],
                    "size": s["size"] or 0,
                    "s3_key": s["s3_key"],
                }
            )
    stale.sort(key=lambda x: x["size"], reverse=True)
    return stale


def print_report(stale: List[dict]) -> int:
    """Print the report. Returns total wasted bytes."""
    if not stale:
        print("\nNo already-organized files found sitting in לפזר folders.")
        return 0

    total = sum(s["size"] for s in stale)
    print(f"\n=== Already-organized files still in לפזר folders ({len(stale)} files) ===\n")
    for s in stale:
        print(f"  {_format_bytes(s['size']):>10}  {s['scattered_path']}")
        for org in s["organized_paths"]:
            print(f"              -> already at: {org}")
    print(f"\nTotal reclaimable: {_format_bytes(total)}")
    return total


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find files in לפזר staging folders that are already organized elsewhere.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", type=Path, nargs="+", metavar="PATH", help="Folder(s) to scan")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    print("AWS Copier — לפזר Duplicate Finder")
    print(f"Scanning: {', '.join(str(p) for p in args.paths)}")

    by_md5 = build_index(args.paths)
    stale = find_scattered_already_organized(by_md5)
    print_report(stale)


if __name__ == "__main__":
    main()
