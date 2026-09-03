"""Standalone tool: reorganize a phone-backup folder into a year/month structure by capture
date, deduplicating exact-content repeats along the way (Phase 1 of 2 — local move only).

Motivation: repeated phone backups (tali_cellphone_bkp_20240528, _20250423, ...) each
re-copy content already backed up in a prior snapshot, wasting hundreds of GB. This tool
moves every media file into <dest-root>/<year>/<month>/, keeping exactly one copy per
distinct MD5 (reused from .milo_backup.info — no re-hashing) and quarantining the rest
under <dest-root>/_duplicates/ instead of deleting them outright.

Date is determined per file, in this order: image EXIF DateTimeOriginal -> date parsed
from the filename -> file mtime (flagged low-confidence) -> _unsorted/ if nothing usable.

This script only moves local files and writes a migration manifest — it never touches S3.
That's intentional: the destination folders have no .milo_backup.info yet, so the daemon's
own next periodic scan re-uploads the kept files at their new S3 keys. Once that's
confirmed to have happened, run cleanup_migrated_s3.py against the manifest this script
writes to delete the now-redundant old S3 objects (duplicates immediately, each kept
file's old key only after its new key is confirmed present).

Dry-run by default — prints the full plan without touching any file. Pass --execute to
actually move files and write the manifest.

    docker exec aws-copier python reorganize_cellphone_backup.py /data/pictures/cellphone_bkp/tali_cellphone
    docker exec aws-copier python reorganize_cellphone_backup.py /data/pictures/cellphone_bkp/tali_cellphone --execute
"""

import argparse
import json
import logging
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Covers IMG_20240528_101112.jpg, VID-20240528-WA0001.mp4, 20240528_101112.jpg,
# WhatsApp Image 2024-05-28 at 10.11.12.jpeg, Screenshot_20240528-101112.png, etc.
_FILENAME_DATE_RE = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")

_EXIF_DATETIME_TAGS = (36867, 306)  # DateTimeOriginal, DateTime
_MANIFEST_FILENAME = "_migration_manifest.jsonl"


@dataclass
class PlanEntry:
    """One file's planned move: where it lives now, where it's going, and why."""

    source: Path
    dest: Path
    md5: Optional[str]
    old_s3_key: Optional[str]
    date_source: str  # exif / filename / mtime / unknown


@dataclass
class Plan:
    """Full migration plan: one kept file per MD5 group, the rest quarantined as duplicates."""

    keeps: List[PlanEntry]
    duplicates: List[PlanEntry]
    skipped_no_md5: List[Path]


def _date_from_exif(path: Path) -> Optional[datetime]:
    """Read EXIF DateTimeOriginal/DateTime from an image file, if Pillow is available."""
    if Image is None or path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            for tag_id in _EXIF_DATETIME_TAGS:
                raw = exif.get(tag_id)
                if raw:
                    return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except Exception as e:
        logger.debug(f"EXIF read failed for {path}: {e}")
    return None


def _date_from_filename(path: Path) -> Optional[datetime]:
    """Parse a YYYYMMDD-shaped date out of the filename, if one looks plausible."""
    match = _FILENAME_DATE_RE.search(path.name)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    if not (2000 <= year <= 2035 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _date_from_mtime(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def determine_date(path: Path) -> Tuple[Optional[datetime], str]:
    """Determine a file's capture date, in order: EXIF -> filename -> mtime -> unknown.

    Args:
        path: Local file path.

    Returns:
        (date, source) where source is one of "exif", "filename", "mtime", "unknown".
        date is None only when source is "unknown".
    """
    date = _date_from_exif(path)
    if date:
        return date, "exif"
    date = _date_from_filename(path)
    if date:
        return date, "filename"
    date = _date_from_mtime(path)
    if date:
        return date, "mtime"
    return None, "unknown"


def load_backup_index(source: Path) -> Dict[Path, Dict[str, Optional[str]]]:
    """Reuse each file's MD5 and recorded S3 key from .milo_backup.info — no re-hashing.

    Args:
        source: Root folder to search for .milo_backup.info files under.

    Returns:
        Mapping of local file Path to {"md5": ..., "s3_key": ...} (s3_key may be None for
        legacy entries recorded before that field existed).
    """
    index: Dict[Path, Dict[str, Optional[str]]] = {}
    for info_file in source.rglob(".milo_backup.info"):
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
            if md5:
                index[folder / filename] = {"md5": md5, "s3_key": s3_key}
    return index


def _unique_destination(dest: Path, used: set) -> Path:
    """Append _1, _2, ... to avoid colliding with an already-planned or existing path."""
    candidate = dest
    i = 1
    while candidate in used or candidate.exists():
        candidate = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        i += 1
    used.add(candidate)
    return candidate


def build_plan(source: Path, dest_root: Path) -> Plan:
    """Build the full move plan: one kept file per MD5 group, duplicates quarantined.

    Args:
        source: Folder to scan recursively for media files.
        dest_root: Root under which <year>/<month>/ (kept) and _duplicates/, _unsorted/
            (quarantined / date-unknown) are created.

    Returns:
        The Plan describing every file's destination.
    """
    backup_index = load_backup_index(source)

    all_files = [
        p for p in source.rglob("*") if p.is_file() and p.name != ".milo_backup.info" and p.suffix.lower() in MEDIA_EXTENSIONS
    ]

    by_md5: Dict[str, List[Path]] = defaultdict(list)
    skipped_no_md5: List[Path] = []
    for path in all_files:
        entry = backup_index.get(path)
        if entry is None or not entry.get("md5"):
            skipped_no_md5.append(path)
            continue
        by_md5[entry["md5"]].append(path)

    used_dest_paths: set = set()
    keeps: List[PlanEntry] = []
    duplicates: List[PlanEntry] = []

    for md5, paths in by_md5.items():
        dated = [(p, *determine_date(p)) for p in paths]
        with_date = [(p, d, src) for p, d, src in dated if d is not None]
        keeper_path, keeper_date, keeper_source = (
            min(with_date, key=lambda t: t[1]) if with_date else dated[0]
        )
        old_s3_key = backup_index[keeper_path]["s3_key"]

        if keeper_date is not None:
            dest_dir = dest_root / f"{keeper_date.year:04d}" / f"{keeper_date.month:02d}"
        else:
            dest_dir = dest_root / "_unsorted"
        dest = _unique_destination(dest_dir / keeper_path.name, used_dest_paths)
        keeps.append(PlanEntry(source=keeper_path, dest=dest, md5=md5, old_s3_key=old_s3_key, date_source=keeper_source))

        for p, d, src in dated:
            if p == keeper_path:
                continue
            dup_dest = _unique_destination(dest_root / "_duplicates" / p.relative_to(source), used_dest_paths)
            duplicates.append(
                PlanEntry(source=p, dest=dup_dest, md5=md5, old_s3_key=backup_index[p]["s3_key"], date_source=src)
            )

    return Plan(keeps=keeps, duplicates=duplicates, skipped_no_md5=skipped_no_md5)


def print_plan_summary(plan: Plan, dest_root: Path) -> None:
    """Print a human-readable summary of the plan (not every single file)."""
    by_source: Dict[str, int] = defaultdict(int)
    for entry in plan.keeps:
        by_source[entry.date_source] += 1

    print(f"\nDestination root: {dest_root}")
    print(f"\nFiles to keep (one per unique MD5): {len(plan.keeps)}")
    for source_kind in ("exif", "filename", "mtime", "unknown"):
        if by_source[source_kind]:
            label = "unsorted (no usable date)" if source_kind == "unknown" else f"dated via {source_kind}"
            print(f"    {by_source[source_kind]:6d}  {label}")

    print(f"\nDuplicate copies to quarantine under _duplicates/: {len(plan.duplicates)}")
    if plan.skipped_no_md5:
        print(f"\nSkipped (no recorded MD5 — not backed up yet, left untouched): {len(plan.skipped_no_md5)}")
        for p in plan.skipped_no_md5[:10]:
            print(f"    {p}")
        if len(plan.skipped_no_md5) > 10:
            print(f"    ... and {len(plan.skipped_no_md5) - 10} more")

    print("\nSample moves:")
    for entry in plan.keeps[:10]:
        print(f"    KEEP  {entry.source} -> {entry.dest}")
    for entry in plan.duplicates[:10]:
        print(f"    DUP   {entry.source} -> {entry.dest}")


def execute_plan(plan: Plan, source: Path, dest_root: Path) -> Path:
    """Physically move every planned file and append a manifest entry for each.

    Args:
        plan: The plan built by build_plan().
        source: The folder being migrated (used to clean up now-empty subdirectories).
        dest_root: Root the manifest file is written under.

    Returns:
        Path to the written manifest (JSON Lines — one entry per moved file).
    """
    manifest_path = dest_root / _MANIFEST_FILENAME
    dest_root.mkdir(parents=True, exist_ok=True)

    with open(manifest_path, "a", encoding="utf-8") as manifest:
        for entry, entry_type in [(e, "keep") for e in plan.keeps] + [(e, "duplicate") for e in plan.duplicates]:
            entry.dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry.source), str(entry.dest))
            manifest.write(
                json.dumps(
                    {
                        "type": entry_type,
                        "md5": entry.md5,
                        "old_local_path": str(entry.source),
                        "new_local_path": str(entry.dest),
                        "old_s3_key": entry.old_s3_key,
                        "done": False,
                    }
                )
                + "\n"
            )

    removed = _remove_empty_dirs(source)
    logger.info(f"Removed {removed} now-empty directories under {source}")
    return manifest_path


def _remove_stale_backup_info(dirpath: Path) -> None:
    """Delete a directory's .milo_backup.info if it's the only thing left in it.

    Every media file it described has been moved elsewhere, so it's pure stale
    bookkeeping at that point. Left alone (not touched) if any other file remains —
    deleting it while un-migrated files are still present would make the daemon treat
    them as brand-new on its next scan and force a needless re-upload.
    """
    info_file = dirpath / ".milo_backup.info"
    if info_file.is_file() and list(dirpath.iterdir()) == [info_file]:
        info_file.unlink()


def _remove_empty_dirs(root: Path) -> int:
    """Remove now-empty directories under root, bottom-up. Returns count removed."""
    removed = 0
    if not root.is_dir():
        return removed
    for dirpath in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if dirpath.is_dir():
            _remove_stale_backup_info(dirpath)
            try:
                dirpath.rmdir()
                removed += 1
            except OSError:
                pass  # Not empty — expected for dirs with leftover non-media files
    _remove_stale_backup_info(root)
    try:
        root.rmdir()
        removed += 1
    except OSError:
        pass
    return removed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reorganize a phone-backup folder into a year/month structure by "
        "capture date, deduplicating exact-content repeats (Phase 1 — local move only; "
        "see cleanup_migrated_s3.py for Phase 2, the S3-side cleanup).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", type=Path, help="Folder to reorganize (scanned recursively)")
    parser.add_argument(
        "--dest-root",
        type=Path,
        default=None,
        metavar="PATH",
        help="Where <year>/<month>/, _duplicates/, _unsorted/ are created (default: source's parent folder)",
    )
    parser.add_argument("--execute", action="store_true", help="Actually move files (default: dry run, plan only)")
    parser.add_argument("--verbose", action="store_true", help="Show debug logging")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if not args.source.is_dir():
        sys.exit(f"Error: source folder not found: {args.source}")
    dest_root = args.dest_root or args.source.parent

    if Image is None:
        logger.warning("Pillow not installed — EXIF dates unavailable, falling back to filename/mtime for images too")

    print("AWS Copier — Cellphone Backup Reorganizer")
    print(f"Source: {args.source}")

    plan = build_plan(args.source, dest_root)
    print_plan_summary(plan, dest_root)

    if not args.execute:
        print("\nDry run only — no files were moved. Pass --execute to apply this plan.")
        return

    manifest_path = execute_plan(plan, args.source, dest_root)
    print(f"\nDone. {len(plan.keeps)} kept, {len(plan.duplicates)} quarantined as duplicates.")
    print(f"Manifest written to: {manifest_path}")
    print("Next: wait for the daemon's next periodic scan to re-upload the kept files at "
          "their new location, then run cleanup_migrated_s3.py against this manifest.")


if __name__ == "__main__":
    main()
