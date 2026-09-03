"""Standalone tool: S3-side cleanup for a migration done by reorganize_cellphone_backup.py
(Phase 2 of 2).

reorganize_cellphone_backup.py only moves files locally — it doesn't touch S3, since the
daemon's own next periodic scan is what re-uploads each kept file at its new location.
This script reads the manifest that Phase 1 wrote and, for each entry:

  - "duplicate" entries: deletes the old S3 object immediately. Safe regardless of timing
    — the content lives on at the kept file's S3 key (old or new), so the duplicate's own
    copy is pure waste the moment it's identified as a duplicate.
  - "keep" entries: deletes the OLD S3 key only after confirming the NEW key already
    exists in S3 (i.e. the daemon has re-uploaded it). If the new key isn't there yet,
    the entry is left alone and will be picked up on a later run of this script — never
    deletes an old copy before the new one is confirmed to exist.

Re-runnable: completed entries are marked "done" in the manifest and skipped on
subsequent runs, so this can be run repeatedly (e.g. once a day) until everything clears.

Needs real S3 credentials, so — unlike the two path-only tools — it does take --config.

Dry-run by default:
    uv run python cleanup_migrated_s3.py --manifest /path/to/_migration_manifest.jsonl --config config.yaml
    uv run python cleanup_migrated_s3.py --manifest ... --config config.yaml --execute
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import load_config

logger = logging.getLogger(__name__)


def _read_manifest(manifest_path: Path) -> List[Dict[str, Any]]:
    entries = []
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _write_manifest(manifest_path: Path, entries: List[Dict[str, Any]]) -> None:
    with open(manifest_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


async def process_manifest(
    entries: List[Dict[str, Any]], s3_manager: S3Manager, execute: bool
) -> Dict[str, int]:
    """Apply the cleanup rules to every pending manifest entry, mutating `entries` in place.

    Args:
        entries: Manifest entries as loaded by _read_manifest() — mutated in place, marking
            "done": True on anything actually (or, in dry-run, that would be) cleaned up.
        s3_manager: Initialized S3Manager to run the checks/deletes against.
        execute: If False, only report what would happen — no S3 calls that mutate state.

    Returns:
        Counts: {"duplicates_deleted", "kept_old_key_deleted", "kept_waiting", "skipped_no_key", "already_done"}
    """
    counts = {"duplicates_deleted": 0, "kept_old_key_deleted": 0, "kept_waiting": 0, "skipped_no_key": 0, "already_done": 0}

    for entry in entries:
        if entry.get("done"):
            counts["already_done"] += 1
            continue

        old_key = entry.get("old_s3_key")
        if not old_key:
            counts["skipped_no_key"] += 1
            continue

        if entry["type"] == "duplicate":
            print(f"DELETE duplicate: {old_key}")
            if execute:
                if await s3_manager.delete_object(old_key):
                    entry["done"] = True
                    counts["duplicates_deleted"] += 1
            else:
                counts["duplicates_deleted"] += 1

        elif entry["type"] == "keep":
            new_key = entry.get("new_s3_key")
            if not new_key:
                counts["skipped_no_key"] += 1
                continue
            new_exists = await s3_manager.check_exists(new_key)
            if not new_exists:
                print(f"WAITING (new key not uploaded yet): {new_key}")
                counts["kept_waiting"] += 1
                continue
            print(f"DELETE old key (new copy confirmed at {new_key}): {old_key}")
            if execute:
                if await s3_manager.delete_object(old_key):
                    entry["done"] = True
                    counts["kept_old_key_deleted"] += 1
            else:
                counts["kept_old_key_deleted"] += 1

    return counts


def _build_s3_key(local_path: Path, config) -> str:
    """Mirror FileListener._build_s3_key's pure logic — no FileListener instance needed."""
    for watch_folder in config.watch_folders:
        try:
            relative_path = local_path.relative_to(watch_folder)
            s3_folder_name = config.get_s3_name_for_folder(watch_folder)
            return f"{s3_folder_name}/{relative_path}".replace("\\", "/")
        except ValueError:
            continue
    return str(local_path).replace("\\", "/")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True, help="Manifest written by reorganize_cellphone_backup.py")
    parser.add_argument("--config", type=Path, required=True, help="config.yaml with S3 credentials")
    parser.add_argument("--execute", action="store_true", help="Actually delete objects (default: dry run)")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if not args.manifest.exists():
        sys.exit(f"Error: manifest not found at {args.manifest}")
    if not args.config.exists():
        sys.exit(f"Error: config not found at {args.config}")

    config = load_config(args.config)
    entries = _read_manifest(args.manifest)

    for entry in entries:
        if entry["type"] == "keep" and "new_s3_key" not in entry:
            entry["new_s3_key"] = _build_s3_key(Path(entry["new_local_path"]), config)

    s3_manager = S3Manager(config)
    await s3_manager.initialize()
    try:
        counts = await process_manifest(entries, s3_manager, args.execute)
    finally:
        await s3_manager.close()

    if args.execute:
        _write_manifest(args.manifest, entries)

    print(f"\n{'Applied' if args.execute else 'Would apply (dry run)'}:")
    print(f"    Duplicate objects deleted: {counts['duplicates_deleted']}")
    print(f"    Kept-file old keys deleted (new copy confirmed): {counts['kept_old_key_deleted']}")
    print(f"    Kept-file entries still waiting on re-upload: {counts['kept_waiting']}")
    print(f"    Already done (from a prior run): {counts['already_done']}")
    if counts["skipped_no_key"]:
        print(f"    Skipped, no recorded S3 key: {counts['skipped_no_key']}")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
