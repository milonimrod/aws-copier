"""Truly async S3 manager using aiobotocore."""

import asyncio
import base64
import contextlib
import hashlib
import logging
from pathlib import Path
import os
from typing import Any, Optional, Dict

from aiobotocore.session import get_session
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError

from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)

# PERF-05: a fixed 300s upload timeout is fine for typical photos/documents but cuts off
# large video files on a slow/congested uplink well before they can finish. 1MB/s is a
# conservative throughput floor — genuinely slower than that should time out rather than
# hang indefinitely — and 300s stays the floor so small-file behavior is unchanged.
_MIN_ASSUMED_UPLOAD_BYTES_PER_SEC = 1024 * 1024
_UPLOAD_TIMEOUT_FLOOR_SECONDS = 300
_UPLOAD_TIMEOUT_BUFFER_SECONDS = 60

# PERF-06: multipart parts were uploaded one at a time, each waiting on the previous part's
# full network round-trip — capping per-file throughput at roughly one 5MB chunk per
# round-trip regardless of actual available bandwidth (observed as a hard ~5MB/s ceiling on
# large video uploads). Uploading up to this many parts concurrently lets a single large
# file actually saturate the connection, matching the concurrency AWS's own CLI/SDK tools
# default to for multipart uploads.
_MULTIPART_CONCURRENCY = 8


def estimate_upload_timeout(file_size: int) -> int:
    """Scale an upload timeout with file size so large files aren't cut off mid-transfer.

    Shared by S3Manager (bounds the actual put_object/copy call) and FileListener (bounds
    the whole MD5 + check_exists + upload sequence for one file) so both layers agree on
    how long a given file is allowed to take.

    Args:
        file_size: Size of the file being uploaded, in bytes.

    Returns:
        Timeout in seconds — never less than the 300s floor used before this existed.
    """
    scaled = file_size // _MIN_ASSUMED_UPLOAD_BYTES_PER_SEC + _UPLOAD_TIMEOUT_BUFFER_SECONDS
    return max(_UPLOAD_TIMEOUT_FLOOR_SECONDS, scaled)


class S3Manager:
    """Truly async S3 manager with upload and existence checking (following production pattern)."""

    def __init__(self, config: SimpleConfig, max_pool_connections: int = 100):
        """Initialize S3 manager with configuration."""
        self.config = config
        self._exit_stack = contextlib.AsyncExitStack()
        self._session = get_session()
        self._s3_client = None
        self._client_config = AioConfig(max_pool_connections=max_pool_connections)

    async def initialize(self) -> None:
        """Initialize async S3 client using production pattern.

        CONFIG-05: When config.use_credential_chain is True, explicit credentials are
        omitted from create_client kwargs so aiobotocore traverses the standard
        botocore provider chain (env vars → ~/.aws/credentials → IAM instance profile).
        """
        try:
            # Test connection first with temporary client
            client_kwargs: Dict[str, Any] = {
                "region_name": self.config.aws_region,
                "config": self._client_config,
            }
            if not self.config.use_credential_chain:
                client_kwargs["aws_access_key_id"] = self.config.aws_access_key_id
                client_kwargs["aws_secret_access_key"] = self.config.aws_secret_access_key
            # else: aiobotocore traverses env vars → ~/.aws/credentials → IAM instance profile

            async with self._session.create_client("s3", **client_kwargs) as test_client:
                await test_client.head_bucket(Bucket=self.config.s3_bucket)

            logger.info(f"S3Manager initialized for bucket: {self.config.s3_bucket}")

        except Exception as e:
            logger.error(f"Failed to initialize S3Manager: {e}")
            raise

    async def ensure_lifecycle_rule(self) -> None:
        """Check or set AbortIncompleteMultipartUpload lifecycle rule on the bucket.

        CONFIG-07: Protects against orphaned multipart upload parts accumulating cost
        when uploads are interrupted (the daemon may be killed mid-upload).

        D-11: Logs warning and returns (never raises) on any error — the daemon must
        continue startup even if lifecycle rule cannot be set.
        D-12: If any AbortIncompleteMultipartUpload rule already exists (any
        DaysAfterInitiation), log info and skip. If other lifecycle rules exist but
        none is AbortIncompleteMultipartUpload, log warning and skip — never overwrite
        externally-set rules with put_bucket_lifecycle_configuration (which replaces
        the entire lifecycle config, not a single rule).

        Returns:
            None
        """
        try:
            client = await self._get_or_create_client()
        except Exception as e:
            logger.warning(
                f"Could not verify multipart lifecycle rule: {e}. "
                f"Incomplete uploads may accumulate cost."
            )
            return

        existing_rules_present = False
        try:
            response = await client.get_bucket_lifecycle_configuration(
                Bucket=self.config.s3_bucket
            )
            for rule in response.get("Rules", []):
                existing_rules_present = True
                abort = rule.get("AbortIncompleteMultipartUpload")
                if abort:
                    days = abort.get("DaysAfterInitiation", "?")
                    logger.info(
                        f"S3 lifecycle rule already present "
                        f"(DaysAfterInitiation={days}). Skipping."
                    )
                    return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "NoSuchLifecycleConfiguration":
                logger.warning(
                    f"Could not verify multipart lifecycle rule: {e}. "
                    f"Incomplete uploads may accumulate cost."
                )
                return
            # NoSuchLifecycleConfiguration → safe to create one
        except Exception as e:
            logger.warning(
                f"Could not verify multipart lifecycle rule: {e}. "
                f"Incomplete uploads may accumulate cost."
            )
            return

        # D-12: never overwrite externally-set rules. If get returned other rules
        # but none is AbortIncompleteMultipartUpload, warn and skip.
        if existing_rules_present:
            logger.warning(
                "Could not verify multipart lifecycle rule: bucket has existing lifecycle "
                "rules but none is AbortIncompleteMultipartUpload. "
                "Incomplete uploads may accumulate cost."
            )
            return

        # No lifecycle config at all → safe to create one.
        try:
            await client.put_bucket_lifecycle_configuration(
                Bucket=self.config.s3_bucket,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "aws-copier-abort-incomplete-multipart",
                            "Status": "Enabled",
                            "Filter": {"Prefix": ""},
                            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                        }
                    ]
                },
            )
            logger.info(
                "S3 lifecycle rule set: AbortIncompleteMultipartUpload after 1 day."
            )
        except Exception as e:
            logger.warning(
                f"Could not verify multipart lifecycle rule: {e}. "
                f"Incomplete uploads may accumulate cost."
            )

    async def _get_or_create_client(self):
        """Get or create S3 client using AsyncExitStack pattern (like your production code).

        CONFIG-05: When config.use_credential_chain is True, explicit credentials are
        omitted from create_client kwargs so aiobotocore traverses the standard
        botocore provider chain (env vars → ~/.aws/credentials → IAM instance profile).
        """
        if not self._exit_stack:
            self._exit_stack = contextlib.AsyncExitStack()
        if not self._s3_client:
            client_kwargs: Dict[str, Any] = {
                "region_name": self.config.aws_region,
                "config": self._client_config,
            }
            if not self.config.use_credential_chain:
                client_kwargs["aws_access_key_id"] = self.config.aws_access_key_id
                client_kwargs["aws_secret_access_key"] = self.config.aws_secret_access_key
            # else: aiobotocore traverses env vars → ~/.aws/credentials → IAM instance profile
            self._s3_client = await self._exit_stack.enter_async_context(
                self._session.create_client("s3", **client_kwargs)
            )
        return self._s3_client

    async def close(self) -> None:
        """Close the S3 manager and cleanup resources using production pattern."""
        if self._s3_client:
            await self._s3_client.close()
            self._s3_client = None
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
        logger.debug("S3Manager closed")

    async def upload_file(
        self,
        local_path: Path,
        s3_key: str,
        precomputed_md5: Optional[str] = None,  # PERF-03 / D-05
    ) -> bool:
        """Upload file to S3 with MD5 checksum verification.

        Args:
            local_path: Path to local file
            s3_key: S3 object key
            precomputed_md5: Pre-computed MD5 hex string. When provided, skips internal
                _calculate_md5 call (PERF-03). None triggers internal computation
                (backward-compatible default).

        Returns:
            True if upload successful, False otherwise
        """
        try:
            if not local_path.exists():
                logger.error(f"File not found: {local_path}")
                return False

            # PERF-03: prefer caller-provided hash to avoid double computation.
            md5_hash = precomputed_md5 or await self._calculate_md5(local_path)
            if not md5_hash:
                logger.error(f"Failed to calculate MD5 for: {local_path}")
                return False

            # Build full S3 key with prefix
            full_s3_key = self._build_s3_key(s3_key)

            # Use chunked reading for memory efficiency (especially on Windows)
            file_size = local_path.stat().st_size

            # For large files (>100MB), use multipart upload
            if file_size > 100 * 1024 * 1024:  # 100MB
                return await self._upload_large_file(local_path, full_s3_key, md5_hash)

            # For smaller files, use regular upload with chunked reading
            client = await self._get_or_create_client()

            # Prepare safe metadata
            metadata = self._prepare_metadata(local_path, md5_hash)

            # Use file object directly instead of reading all into memory
            with open(local_path, "rb") as f:
                # PERF-05: timeout scales with file size (see estimate_upload_timeout)
                # instead of a fixed 300s, so larger single-part uploads on a slow
                # connection aren't cut off before they can finish.
                await asyncio.wait_for(
                    client.put_object(
                        Bucket=self.config.s3_bucket,
                        Key=full_s3_key,
                        Body=f,
                        Metadata=metadata,
                    ),
                    timeout=estimate_upload_timeout(file_size),
                )

            # Verify upload by checking MD5
            if await self.check_exists(s3_key, md5_hash):
                logger.debug(f"Upload successful: {local_path} -> s3://{self.config.s3_bucket}/{full_s3_key}")
                return True
            logger.error(f"Upload verification failed for: {local_path}")
            return False

        except Exception as e:
            logger.error(f"Upload failed for {local_path}: {e}")
            return False

    async def check_exists(self, s3_key: str, expected_md5: Optional[str] = None) -> bool:
        """Check if file exists in S3 with optional MD5 verification.

        Args:
            s3_key: S3 object key
            expected_md5: Optional MD5 hash to verify against

        Returns:
            True if file exists (and MD5 matches if provided), False otherwise
        """
        try:
            full_s3_key = self._build_s3_key(s3_key)

            # Use aiobotocore for truly async operation
            client = await self._get_or_create_client()

            # Add timeout to prevent hanging
            response = await asyncio.wait_for(
                client.head_object(Bucket=self.config.s3_bucket, Key=full_s3_key),
                timeout=30,  # 30 second timeout for existence checks
            )

            # If no MD5 check requested, just return True (file exists)
            if expected_md5 is None:
                return True

            # Check MD5 in metadata
            metadata = response.get("Metadata", {})
            stored_md5 = metadata.get("md5-checksum")

            if stored_md5:
                return stored_md5 == expected_md5
            etag = response.get("ETag", "").strip('"')
            if "-" not in etag:  # Simple upload, ETag is MD5
                return etag == expected_md5
            logger.warning(f"Cannot verify MD5 for multipart upload: {s3_key}")
            return True  # Assume it's correct

        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False  # File doesn't exist
            logger.error(f"Error checking S3 object existence: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error checking S3 object: {e}")
            return False

    async def _calculate_md5(self, file_path: Path) -> Optional[str]:
        """Calculate MD5 hash of a file using async I/O.

        Args:
            file_path: Path to file

        Returns:
            MD5 hash as hex string, or None if error
        """
        try:
            hasher = hashlib.md5()

            # Use asyncio to run in thread pool for file I/O
            def _hash_file():
                with open(file_path, "rb") as f:
                    while chunk := f.read(8192):
                        hasher.update(chunk)
                return hasher.hexdigest()

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _hash_file)

        except Exception as e:
            logger.error(f"Error calculating MD5 for {file_path}: {e}")
            return None

    def _build_s3_key(self, s3_key: str) -> str:
        """Build full S3 key with prefix.

        Args:
            s3_key: Relative S3 key

        Returns:
            Full S3 key with prefix
        """
        if self.config.s3_prefix:
            return f"{self.config.s3_prefix.rstrip('/')}/{s3_key}"
        return s3_key

    def _encode_metadata_value(self, value: str) -> str:
        """Encode metadata value to be S3-safe (ASCII only).

        Args:
            value: Original metadata value

        Returns:
            ASCII-safe metadata value (base64 encoded if needed)
        """
        try:
            # Try to encode as ASCII
            value.encode("ascii")
            return value
        except UnicodeEncodeError:
            # If non-ASCII characters, base64 encode
            encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
            return f"base64:{encoded}"

    def _decode_metadata_value(self, value: str) -> str:
        """Decode metadata value from S3-safe format.

        Args:
            value: Potentially encoded metadata value

        Returns:
            Original metadata value
        """
        if value.startswith("base64:"):
            # Decode base64
            encoded_part = value[7:]  # Remove "base64:" prefix
            try:
                return base64.b64decode(encoded_part).decode("utf-8")
            except Exception:
                # If decoding fails, return as-is
                return value
        return value

    def _prepare_metadata(self, local_path: Path, md5_hash: str) -> Dict[str, str]:
        """Prepare metadata dictionary with safe encoding.

        Args:
            local_path: Local file path
            md5_hash: MD5 hash of the file

        Returns:
            Dictionary of safely encoded metadata
        """
        return {
            "md5-checksum": md5_hash,
            "original-path": self._encode_metadata_value(str(local_path)),
            "file-size": str(local_path.stat().st_size),
        }

    async def _upload_part(self, client: Any, s3_key: str, upload_id: str, part_number: int, chunk: bytes) -> Dict[str, Any]:
        """Upload a single multipart part.

        Args:
            client: Active aiobotocore S3 client
            s3_key: S3 key (with prefix) of the object being uploaded
            upload_id: Multipart upload ID from create_multipart_upload
            part_number: 1-based part number
            chunk: Part body bytes

        Returns:
            {"ETag": ..., "PartNumber": ...} for use in complete_multipart_upload's Parts list.
        """
        part_response = await asyncio.wait_for(
            client.upload_part(
                Bucket=self.config.s3_bucket,
                Key=s3_key,
                PartNumber=part_number,
                UploadId=upload_id,
                Body=chunk,
            ),
            timeout=300,  # generous per-5MB-part timeout, unrelated to total file size
        )
        return {"ETag": part_response["ETag"], "PartNumber": part_number}

    async def _upload_large_file(self, local_path: Path, s3_key: str, md5_hash: str) -> bool:
        """Upload large file using multipart upload, with parts sent concurrently (PERF-06).

        PERF-06: parts used to be uploaded strictly one at a time, each waiting on the full
        network round-trip of the previous part — capping per-file throughput at roughly one
        5MB chunk per round-trip regardless of actual available bandwidth. Uses a sliding
        window (same pattern as upload_large.py's _multipart_upload) of up to
        _MULTIPART_CONCURRENCY parts in flight at once: as soon as one completes, the next
        chunk is read and dispatched, so memory stays bounded to concurrency × 5MB however
        large the file is.

        Args:
            local_path: Path to local file
            s3_key: S3 key (with prefix)
            md5_hash: MD5 hash of the file

        Returns:
            True if upload successful, False otherwise
        """
        try:
            client = await self._get_or_create_client()
            metadata = self._prepare_metadata(local_path, md5_hash)

            # Start multipart upload
            response = await client.create_multipart_upload(
                Bucket=self.config.s3_bucket,
                Key=s3_key,
                Metadata=metadata,
            )
            upload_id = response["UploadId"]

            chunk_size = 5 * 1024 * 1024  # 5MB
            completed: Dict[int, Dict[str, Any]] = {}
            active: Dict[asyncio.Task, int] = {}

            try:
                with open(local_path, "rb") as f:
                    part_number = 0
                    eof = False

                    while active or not eof:
                        # Fill concurrency slots
                        while not eof and len(active) < _MULTIPART_CONCURRENCY:
                            chunk = await asyncio.to_thread(f.read, chunk_size)
                            if not chunk:
                                eof = True
                                break
                            part_number += 1
                            task = asyncio.create_task(self._upload_part(client, s3_key, upload_id, part_number, chunk))
                            active[task] = part_number

                        if not active:
                            break

                        # Wait for the first part to finish, then loop to submit the next chunk
                        done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            pn = active.pop(task)
                            completed[pn] = task.result()  # propagates exception on failure

                            if pn % 10 == 0:
                                logger.debug(f"Uploaded {pn} parts for {local_path}")

                # complete_multipart_upload requires parts in ascending PartNumber order.
                ordered_parts = [completed[i] for i in range(1, part_number + 1)]
                await client.complete_multipart_upload(
                    Bucket=self.config.s3_bucket,
                    Key=s3_key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": ordered_parts},
                )

                logger.debug(f"Large file upload successful: {local_path} -> s3://{self.config.s3_bucket}/{s3_key}")
                return True

            except Exception as e:
                # Cancel any still-in-flight part tasks before aborting so their exceptions
                # don't surface as "Task exception was never retrieved" warnings.
                for remaining in list(active):
                    remaining.cancel()
                if active:
                    await asyncio.gather(*active, return_exceptions=True)
                # Abort multipart upload on error
                try:
                    await client.abort_multipart_upload(
                        Bucket=self.config.s3_bucket,
                        Key=s3_key,
                        UploadId=upload_id,
                    )
                except Exception:
                    pass  # Ignore abort errors
                raise e

        except Exception as e:
            logger.error(f"Large file upload failed for {local_path}: {e}")
            return False

    async def get_object_info(self, s3_key: str) -> Optional[dict]:
        """Get S3 object information using async operations.

        Args:
            s3_key: S3 object key

        Returns:
            Object metadata dict or None if not found
        """
        try:
            full_s3_key = self._build_s3_key(s3_key)

            client = await self._get_or_create_client()
            response = await client.head_object(Bucket=self.config.s3_bucket, Key=full_s3_key)

            return {
                "size": response.get("ContentLength", 0),
                "last_modified": response.get("LastModified"),
                "etag": response.get("ETag", "").strip('"'),
                "metadata": response.get("Metadata", {}),
            }

        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return None
            logger.error(f"Error getting object info: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting object info: {e}")
            return None

    async def move_object(self, source_key: str, dest_key: str) -> bool:
        """Move an object within the bucket via server-side CopyObject, then delete the source.

        Used when a file's content is unchanged but its S3 key changed — typically because
        the local directory it lives in was renamed or moved. A server-side copy avoids
        re-uploading the file's bytes; MetadataDirective="COPY" preserves the original
        md5-checksum metadata so the moved object still verifies correctly.

        Args:
            source_key: Current (prefix-less) S3 key the object lives at.
            dest_key: New (prefix-less) S3 key it should live at.

        Returns:
            True if the object was copied to dest_key and the source key removed,
            False on any failure (including a source object over the 5GB single-call
            CopyObject limit, or the source object not existing).
        """
        try:
            info = await self.get_object_info(source_key)
            if info is None:
                logger.warning(f"move_object: source key not found in S3: {source_key}")
                return False

            if info["size"] > 5 * 1024 * 1024 * 1024:  # CopyObject single-call limit
                logger.warning(
                    f"move_object: {source_key} exceeds the 5GB single-call CopyObject "
                    f"limit; multipart copy is not implemented, skipping server-side move"
                )
                return False

            full_source_key = self._build_s3_key(source_key)
            full_dest_key = self._build_s3_key(dest_key)
            client = await self._get_or_create_client()

            await asyncio.wait_for(
                client.copy_object(
                    Bucket=self.config.s3_bucket,
                    CopySource={"Bucket": self.config.s3_bucket, "Key": full_source_key},
                    Key=full_dest_key,
                    MetadataDirective="COPY",
                ),
                timeout=300,
            )
            await asyncio.wait_for(
                client.delete_object(Bucket=self.config.s3_bucket, Key=full_source_key),
                timeout=30,
            )
            logger.debug(f"Moved S3 object: {full_source_key} -> {full_dest_key}")
            return True

        except Exception as e:
            logger.error(f"Failed to move S3 object {source_key} -> {dest_key}: {e}")
            return False


async def main():
    from dotenv import load_dotenv

    load_dotenv(override=True)

    # Build config from environment variables
    config_kwargs = {
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", ""),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        "aws_region": os.environ.get("AWS_REGION", "us-east-1"),
        "s3_bucket": os.environ.get("S3_BUCKET", ""),
        "s3_prefix": os.environ.get("S3_PREFIX", ""),
    }
    config = SimpleConfig(**config_kwargs)

    # Print config for verification
    print("Loaded config:", config.to_dict())

    # Initialize S3Manager
    s3_manager = S3Manager(config)
    await s3_manager.initialize()
    print("Initialized S3Manager:", s3_manager)
    # Write data to /tmp/test
    test_file_path = Path("/tmp/test")
    with open(test_file_path, "w") as f:
        f.write("Hello from aws_copier/core/s3_manager.py!\n")
    md5_hash = await s3_manager._calculate_md5(test_file_path)
    # Upload it to S3 as "test"
    await s3_manager.upload_file(test_file_path, "test")
    print(f"check_exists: {await s3_manager.check_exists('test', md5_hash)}")
    await s3_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
