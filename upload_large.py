"""One-shot uploader for large files (30–50 GB) to S3.

Parts are uploaded concurrently (sliding window) so a gigabit connection can
be saturated instead of being limited by a single serial TCP stream.
No per-file timeout — only per-part, sized to the chunk.
"""

import argparse
import asyncio
import contextlib
import hashlib
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tqdm import tqdm
from aiobotocore.session import get_session
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError

from aws_copier.models.simple_config import DEFAULT_CONFIG_PATH, SimpleConfig, load_config

logger = logging.getLogger(__name__)

DEFAULT_PART_MB = 256  # 256 MB → max ~200 parts for a 50 GB file
DEFAULT_CONCURRENCY = 4  # 4 concurrent part uploads
# 30 min for a 256 MB part → covers connections down to ~140 KB/s
PART_TIMEOUT_PER_MB = 7  # seconds per MB in part (7 s/MB × 256 MB = 1792 s ≈ 30 min)
HEAD_TIMEOUT = 60


def _part_timeout(part_size: int) -> float:
    """Return per-part upload timeout proportional to chunk size."""
    return max(300.0, PART_TIMEOUT_PER_MB * part_size / (1024 * 1024))


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} PB"


class LargeFileUploader:
    """Uploads large files to S3 using concurrent multipart upload with per-part retry."""

    def __init__(
        self,
        config: SimpleConfig,
        retries: int = 3,
        part_size: int = DEFAULT_PART_MB * 1024 * 1024,
        concurrency: int = DEFAULT_CONCURRENCY,
    ):
        self.config = config
        self.retries = retries
        self.part_size = part_size
        self.concurrency = concurrency
        # Bound concurrent MD5 reads to avoid disk thrash; 4 is plenty for large files
        self._md5_semaphore = asyncio.Semaphore(4)
        self._session = get_session()
        self._exit_stack = contextlib.AsyncExitStack()
        self._client = None

    async def _get_client(self) -> Any:
        if self._client is None:
            kwargs: Dict[str, Any] = {
                "region_name": self.config.aws_region,
                "config": AioConfig(max_pool_connections=max(10, self.concurrency * 2)),
            }
            if not self.config.use_credential_chain:
                kwargs["aws_access_key_id"] = self.config.aws_access_key_id
                kwargs["aws_secret_access_key"] = self.config.aws_secret_access_key
            self._client = await self._exit_stack.enter_async_context(self._session.create_client("s3", **kwargs))
        return self._client

    async def close(self) -> None:
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._client = None

    def _full_key(self, s3_key: str) -> str:
        if self.config.s3_prefix:
            return f"{self.config.s3_prefix.rstrip('/')}/{s3_key}"
        return s3_key

    async def _compute_md5(self, path: Path) -> str:
        loop = asyncio.get_running_loop()

        def _hash() -> str:
            h = hashlib.md5()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()

        return await loop.run_in_executor(None, _hash)

    async def _compute_md5_bounded(self, path: Path) -> str:
        """Compute MD5 with semaphore so at most 4 large files are read concurrently."""
        async with self._md5_semaphore:
            return await self._compute_md5(path)

    async def _exists_in_s3(self, full_key: str) -> bool:
        client = await self._get_client()
        try:
            await asyncio.wait_for(
                client.head_object(Bucket=self.config.s3_bucket, Key=full_key),
                timeout=HEAD_TIMEOUT,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    async def _upload_part(
        self,
        client: Any,
        full_key: str,
        upload_id: str,
        part_number: int,
        data: bytes,
    ) -> Dict[str, Any]:
        """Upload one part with exponential-backoff retry."""
        timeout = _part_timeout(len(data))
        for attempt in range(1, self.retries + 2):
            try:
                resp = await asyncio.wait_for(
                    client.upload_part(
                        Bucket=self.config.s3_bucket,
                        Key=full_key,
                        PartNumber=part_number,
                        UploadId=upload_id,
                        Body=data,
                    ),
                    timeout=timeout,
                )
                return {"ETag": resp["ETag"], "PartNumber": part_number}
            except Exception as exc:
                if attempt > self.retries:
                    raise
                wait = 5 * (2 ** (attempt - 1))
                logger.warning(
                    "Part %d attempt %d failed: %s — retrying in %ds",
                    part_number,
                    attempt,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
        raise RuntimeError(f"Part {part_number} exhausted all retries")  # unreachable

    async def upload_file(self, local_path: Path, s3_key: str, md5: Optional[str] = None) -> bool:
        """Upload one file using concurrent multipart upload, retrying the full sequence on error.

        Args:
            local_path: Local file to upload.
            s3_key: Destination S3 key (relative to config prefix).
            md5: Pre-computed MD5 hex string. When None, computed on the fly.

        Returns:
            True on success, False after exhausting retries.
        """
        full_key = self._full_key(s3_key)

        print(f"\n{'─' * 60}")
        print(f"File : {local_path.name}  ({_format_bytes(local_path.stat().st_size)})")
        print(f"Key  : s3://{self.config.s3_bucket}/{full_key}")

        if await self._exists_in_s3(full_key):
            print("Skip : already exists in S3")
            return True

        if md5 is None:
            print("MD5  : computing...", end=" ", flush=True)
            md5 = await self._compute_md5(local_path)
            print(md5)
        else:
            print(f"MD5  : {md5} (pre-computed)")

        file_size = local_path.stat().st_size
        total_parts = max(1, (file_size + self.part_size - 1) // self.part_size)

        for attempt in range(1, self.retries + 2):
            try:
                await self._multipart_upload(local_path, full_key, md5, file_size, total_parts)
                return True
            except Exception as exc:
                if attempt > self.retries:
                    logger.error("Upload of %s failed after all retries: %s", local_path.name, exc)
                    return False
                wait = 5 * (2 ** (attempt - 1))
                logger.warning("File attempt %d failed: %s — retrying in %ds", attempt, exc, wait)
                await asyncio.sleep(wait)

        return False

    async def _multipart_upload(
        self,
        local_path: Path,
        full_key: str,
        md5: str,
        file_size: int,
        total_parts: int,
    ) -> None:
        """Run a complete multipart upload with `self.concurrency` parallel part uploads.

        Uses a sliding window: as soon as one part finishes, the next chunk is read
        and dispatched, so memory usage is bounded to concurrency × part_size.
        """
        client = await self._get_client()

        resp = await client.create_multipart_upload(
            Bucket=self.config.s3_bucket,
            Key=full_key,
            Metadata={
                "md5-checksum": md5,
                "file-size": str(file_size),
                "original-name": local_path.name,
            },
        )
        upload_id = resp["UploadId"]
        # part_num -> ETag dict, filled as parts complete
        completed: Dict[int, Dict[str, Any]] = {}

        try:
            with (
                open(local_path, "rb") as f,
                tqdm(
                    total=file_size,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=local_path.name,
                    ncols=80,
                ) as bar,
            ):
                # active maps asyncio.Task -> (part_num, chunk_len)
                active: Dict["asyncio.Task[Dict[str, Any]]", Tuple[int, int]] = {}
                part_num = 0
                eof = False

                while active or not eof:
                    # Fill concurrency slots
                    while not eof and len(active) < self.concurrency:
                        chunk = f.read(self.part_size)
                        if not chunk:
                            eof = True
                            break
                        part_num += 1
                        task: asyncio.Task[Dict[str, Any]] = asyncio.create_task(
                            self._upload_part(client, full_key, upload_id, part_num, chunk)
                        )
                        active[task] = (part_num, len(chunk))

                    if not active:
                        break

                    # Wait for the first part to finish, then loop to submit the next chunk
                    done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        pn, sz = active.pop(task)
                        completed[pn] = task.result()  # propagates exception on failure
                        bar.update(sz)

            ordered_parts = [completed[i] for i in range(1, part_num + 1)]
            await client.complete_multipart_upload(
                Bucket=self.config.s3_bucket,
                Key=full_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": ordered_parts},
            )

        except Exception:
            # Cancel any in-flight part tasks before aborting so their exceptions
            # don't surface as "Task exception was never retrieved" warnings.
            for remaining in list(active):
                remaining.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            try:
                await client.abort_multipart_upload(Bucket=self.config.s3_bucket, Key=full_key, UploadId=upload_id)
            except Exception:
                pass
            raise

    async def upload_folder(self, folder: Path, s3_dest: Optional[str]) -> bool:
        """Upload every regular file in *folder* sequentially.

        Returns:
            True if all files succeeded.
        """
        files = sorted(p for p in folder.rglob("*") if p.is_file())
        if not files:
            print(f"No files found in {folder}")
            return True

        total_size = sum(p.stat().st_size for p in files)
        print(f"Found {len(files)} file(s) — total {_format_bytes(total_size)}")

        # Pre-compute all MD5s concurrently (bounded to 4 at a time to avoid disk thrash)
        print(f"Pre-computing MD5 hashes for {len(files)} file(s)...")
        md5s: List[str] = await asyncio.gather(*[self._compute_md5_bounded(fp) for fp in files])
        print("MD5 hashes ready.\n")

        start = time.monotonic()
        failed: List[Path] = []

        for fp, md5 in zip(files, md5s):
            # Preserve subdirectory structure relative to the root folder
            relative = "/".join(fp.relative_to(folder).parts)
            s3_key = f"{s3_dest.rstrip('/')}/{relative}" if s3_dest else relative
            ok = await self.upload_file(fp, s3_key, md5=md5)
            if not ok:
                failed.append(fp)

        elapsed = time.monotonic() - start
        print(f"\n{'─' * 60}")
        print(f"Done: {len(files) - len(failed)}/{len(files)} succeeded in {elapsed:.0f}s")

        if failed:
            print("Failed:")
            for fp in failed:
                print(f"  {fp}")

        return not failed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload large files (30–50 GB) from a folder to S3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python upload_large.py /data/archives
  uv run python upload_large.py /data/archives --s3-dest backups/archives
  uv run python upload_large.py /data/archives --config ~/my-config.yaml --part-size-mb 512
""",
    )
    parser.add_argument("folder", type=Path, help="Folder containing files to upload")
    parser.add_argument(
        "--s3-dest",
        default=None,
        metavar="PREFIX",
        help="S3 key prefix appended after config s3_prefix (default: filename only)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"Config YAML path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--retries", type=int, default=3, help="Retry attempts per part and per file (default: 3)")
    parser.add_argument(
        "--part-size-mb",
        type=int,
        default=DEFAULT_PART_MB,
        help=f"Multipart chunk size in MB (default: {DEFAULT_PART_MB}). Min 5 MB (S3 limit).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Parallel part uploads per file (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument("--verbose", action="store_true", help="Show debug logging")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if not args.folder.is_dir():
        sys.exit(f"Error: {args.folder} is not a directory")

    part_mb = max(5, args.part_size_mb)  # S3 minimum is 5 MB
    if part_mb != args.part_size_mb:
        print("Warning: part size clamped to 5 MB (S3 minimum)")

    config_path = args.config or DEFAULT_CONFIG_PATH
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        sys.exit(f"Error: config not found at {config_path}")

    print("AWS Copier — Large File Uploader")
    print(f"Bucket      : s3://{config.s3_bucket}")
    print(f"Region      : {config.aws_region}")
    if config.s3_prefix:
        print(f"Prefix      : {config.s3_prefix}")
    print(f"Part size   : {part_mb} MB  |  Concurrency: {args.concurrency}  |  Retries: {args.retries}")

    uploader = LargeFileUploader(
        config,
        retries=args.retries,
        part_size=part_mb * 1024 * 1024,
        concurrency=args.concurrency,
    )

    async def run() -> bool:
        try:
            return await uploader.upload_folder(args.folder, args.s3_dest)
        finally:
            await uploader.close()

    ok = asyncio.run(run())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
