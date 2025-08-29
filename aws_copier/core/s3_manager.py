"""Truly async S3 manager using aiobotocore."""

import asyncio
import contextlib
import hashlib
import logging
from pathlib import Path
import os
from typing import Optional

from aiobotocore.session import get_session
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError

from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)


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
        """Initialize async S3 client using production pattern."""
        try:
            # Test connection first with temporary client
            async with self._session.create_client(
                "s3",
                aws_access_key_id=self.config.aws_access_key_id,
                aws_secret_access_key=self.config.aws_secret_access_key,
                region_name=self.config.aws_region,
                config=self._client_config,
            ) as test_client:
                await test_client.head_bucket(Bucket=self.config.s3_bucket)

            logger.info(f"S3Manager initialized for bucket: {self.config.s3_bucket}")

        except Exception as e:
            logger.error(f"Failed to initialize S3Manager: {e}")
            raise

    async def _get_or_create_client(self):
        """Get or create S3 client using AsyncExitStack pattern (like your production code)."""
        if not self._exit_stack:
            self._exit_stack = contextlib.AsyncExitStack()
        if not self._s3_client:
            self._s3_client = await self._exit_stack.enter_async_context(
                self._session.create_client(
                    "s3",
                    aws_access_key_id=self.config.aws_access_key_id,
                    aws_secret_access_key=self.config.aws_secret_access_key,
                    region_name=self.config.aws_region,
                    config=self._client_config,
                )
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

    async def upload_file(self, local_path: Path, s3_key: str) -> bool:
        """Upload file to S3 with MD5 checksum verification.

        Args:
            local_path: Path to local file
            s3_key: S3 object key

        Returns:
            True if upload successful, False otherwise
        """
        try:
            if not local_path.exists():
                logger.error(f"File not found: {local_path}")
                return False

            # Calculate MD5 checksum
            md5_hash = await self._calculate_md5(local_path)
            if not md5_hash:
                logger.error(f"Failed to calculate MD5 for: {local_path}")
                return False

            # Build full S3 key with prefix
            full_s3_key = self._build_s3_key(s3_key)

            # Upload file using aiobotocore (truly async)
            with open(local_path, "rb") as f:
                file_data = f.read()

            client = await self._get_or_create_client()
            await client.put_object(
                Bucket=self.config.s3_bucket,
                Key=full_s3_key,
                Body=file_data,
                Metadata={
                    "md5-checksum": md5_hash,
                    "original-path": str(local_path),
                    "file-size": str(local_path.stat().st_size),
                },
            )

            # Verify upload by checking MD5
            if await self.check_exists(s3_key, md5_hash):
                logger.info(f"Upload successful: {local_path} -> s3://{self.config.s3_bucket}/{full_s3_key}")
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

            response = await client.head_object(Bucket=self.config.s3_bucket, Key=full_s3_key)

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

            loop = asyncio.get_event_loop()
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
