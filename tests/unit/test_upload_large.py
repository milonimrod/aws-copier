"""Tests for the upload_large one-shot large-file uploader."""

import hashlib
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import ClientError

from aws_copier.models.simple_config import SimpleConfig
from upload_large import LargeFileUploader, _format_bytes, _part_timeout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> SimpleConfig:
    defaults = dict(
        aws_access_key_id="key",
        aws_secret_access_key="secret",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        s3_prefix="",
    )
    defaults.update(kwargs)
    return SimpleConfig(**defaults)


def _make_uploader(**kwargs) -> LargeFileUploader:
    config = kwargs.pop("config", _make_config())
    return LargeFileUploader(config, part_size=5 * 1024 * 1024, concurrency=2, **kwargs)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "HeadObject")


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


def test_format_bytes_scales():
    assert _format_bytes(500) == "500.0 B"
    assert _format_bytes(1024) == "1.0 KB"
    assert _format_bytes(1024 * 1024) == "1.0 MB"
    assert _format_bytes(2 * 1024**3) == "2.0 GB"


def test_part_timeout_proportional():
    t_100mb = _part_timeout(100 * 1024 * 1024)
    t_256mb = _part_timeout(256 * 1024 * 1024)
    assert t_256mb > t_100mb
    assert t_100mb >= 300  # always at least 5 min


def test_full_key_no_prefix():
    uploader = _make_uploader(config=_make_config(s3_prefix=""))
    assert uploader._full_key("file.bin") == "file.bin"


def test_full_key_with_prefix():
    uploader = _make_uploader(config=_make_config(s3_prefix="backups"))
    assert uploader._full_key("file.bin") == "backups/file.bin"


def test_full_key_strips_trailing_slash():
    uploader = _make_uploader(config=_make_config(s3_prefix="backups/"))
    assert uploader._full_key("file.bin") == "backups/file.bin"


# ---------------------------------------------------------------------------
# _compute_md5
# ---------------------------------------------------------------------------


async def test_compute_md5_correct():
    uploader = _make_uploader()
    content = b"hello large file"
    expected = hashlib.md5(content).hexdigest()

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(content)
        path = Path(f.name)

    try:
        result = await uploader._compute_md5(path)
        assert result == expected
    finally:
        path.unlink()


# ---------------------------------------------------------------------------
# _exists_in_s3
# ---------------------------------------------------------------------------


async def test_exists_in_s3_true():
    uploader = _make_uploader()
    mock_client = AsyncMock()
    mock_client.head_object = AsyncMock(return_value={})

    with patch.object(uploader, "_get_client", return_value=mock_client):
        assert await uploader._exists_in_s3("some/key") is True


async def test_exists_in_s3_false_on_404():
    uploader = _make_uploader()
    mock_client = AsyncMock()
    mock_client.head_object = AsyncMock(side_effect=_client_error("404"))

    with patch.object(uploader, "_get_client", return_value=mock_client):
        assert await uploader._exists_in_s3("some/key") is False


async def test_exists_in_s3_raises_on_other_error():
    uploader = _make_uploader()
    mock_client = AsyncMock()
    mock_client.head_object = AsyncMock(side_effect=_client_error("AccessDenied"))

    with patch.object(uploader, "_get_client", return_value=mock_client):
        with pytest.raises(ClientError):
            await uploader._exists_in_s3("some/key")


# ---------------------------------------------------------------------------
# _upload_part  (retry behaviour)
# ---------------------------------------------------------------------------


async def test_upload_part_succeeds_first_try():
    uploader = _make_uploader(retries=3)
    mock_client = AsyncMock()
    mock_client.upload_part = AsyncMock(return_value={"ETag": '"abc123"'})

    result = await uploader._upload_part(mock_client, "key", "uid", 1, b"data")

    assert result == {"ETag": '"abc123"', "PartNumber": 1}
    mock_client.upload_part.assert_called_once()


async def test_upload_part_retries_then_succeeds():
    uploader = _make_uploader(retries=3)
    mock_client = AsyncMock()
    mock_client.upload_part = AsyncMock(side_effect=[RuntimeError("transient"), {"ETag": '"ok"'}])

    with patch("upload_large.asyncio.sleep", new_callable=AsyncMock):
        result = await uploader._upload_part(mock_client, "key", "uid", 1, b"data")

    assert result["ETag"] == '"ok"'
    assert mock_client.upload_part.call_count == 2


async def test_upload_part_raises_after_exhausting_retries():
    uploader = _make_uploader(retries=2)
    mock_client = AsyncMock()
    mock_client.upload_part = AsyncMock(side_effect=RuntimeError("always fails"))

    with patch("upload_large.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="always fails"):
            await uploader._upload_part(mock_client, "key", "uid", 1, b"data")

    # 1 initial + 2 retries = 3 total calls
    assert mock_client.upload_part.call_count == 3


# ---------------------------------------------------------------------------
# upload_file — skip if already exists
# ---------------------------------------------------------------------------


async def test_upload_file_skips_existing(tmp_path, capsys):
    fp = tmp_path / "big.bin"
    fp.write_bytes(b"x" * 100)

    uploader = _make_uploader()

    with patch.object(uploader, "_exists_in_s3", new_callable=AsyncMock, return_value=True):
        ok = await uploader.upload_file(fp, "big.bin")

    assert ok is True
    assert "Skip" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# upload_file — full multipart path
# ---------------------------------------------------------------------------


async def test_upload_file_multipart_success(tmp_path):
    content = b"A" * (6 * 1024 * 1024)  # 6 MB → 2 parts at 5 MB chunk
    fp = tmp_path / "data.bin"
    fp.write_bytes(content)

    uploader = _make_uploader()
    mock_client = AsyncMock()

    # HEAD → 404 (file not yet uploaded)
    mock_client.head_object = AsyncMock(side_effect=_client_error("404"))
    mock_client.create_multipart_upload = AsyncMock(return_value={"UploadId": "uid-1"})
    mock_client.upload_part = AsyncMock(side_effect=[{"ETag": '"e1"'}, {"ETag": '"e2"'}])
    mock_client.complete_multipart_upload = AsyncMock(return_value={})

    with patch.object(uploader, "_get_client", new_callable=AsyncMock, return_value=mock_client):
        ok = await uploader.upload_file(fp, "data.bin")

    assert ok is True
    assert mock_client.create_multipart_upload.called
    assert mock_client.upload_part.call_count == 2
    assert mock_client.complete_multipart_upload.called

    # Verify parts were passed in order
    complete_call = mock_client.complete_multipart_upload.call_args
    parts = complete_call.kwargs["MultipartUpload"]["Parts"]
    assert [p["PartNumber"] for p in parts] == [1, 2]


# ---------------------------------------------------------------------------
# upload_file — abort on part failure
# ---------------------------------------------------------------------------


async def test_upload_file_aborts_multipart_on_failure(tmp_path):
    fp = tmp_path / "data.bin"
    fp.write_bytes(b"B" * (6 * 1024 * 1024))

    uploader = _make_uploader(retries=0)  # no retries so failure is immediate
    mock_client = AsyncMock()
    mock_client.head_object = AsyncMock(side_effect=_client_error("404"))
    mock_client.create_multipart_upload = AsyncMock(return_value={"UploadId": "uid-2"})
    mock_client.upload_part = AsyncMock(side_effect=RuntimeError("network error"))
    mock_client.abort_multipart_upload = AsyncMock(return_value={})

    with patch.object(uploader, "_get_client", new_callable=AsyncMock, return_value=mock_client):
        with patch("upload_large.asyncio.sleep", new_callable=AsyncMock):
            ok = await uploader.upload_file(fp, "data.bin")

    assert ok is False
    mock_client.abort_multipart_upload.assert_called_once_with(
        Bucket="test-bucket",
        Key="data.bin",
        UploadId="uid-2",
    )


# ---------------------------------------------------------------------------
# _process_file — pipeline: MD5 → exists-check → upload
# ---------------------------------------------------------------------------


async def test_process_file_hashes_then_uploads(tmp_path):
    fp = tmp_path / "data.bin"
    fp.write_bytes(b"x" * 100)

    uploader = _make_uploader()
    order: list = []

    async def _fake_md5(path: Path) -> str:
        order.append("md5")
        return "deadbeef"

    async def _fake_exists(key: str) -> bool:
        order.append("exists")
        return False

    async def _fake_upload(path: Path, key: str, md5: Optional[str] = None) -> bool:
        order.append("upload")
        assert md5 == "deadbeef"
        return True

    with (
        patch.object(uploader, "_compute_md5", side_effect=_fake_md5),
        patch.object(uploader, "_exists_in_s3", side_effect=_fake_exists),
        patch.object(uploader, "upload_file", side_effect=_fake_upload),
    ):
        ok = await uploader._process_file(fp, "data.bin")

    assert ok is True
    assert order == ["md5", "exists", "upload"]


async def test_process_file_skips_upload_when_exists(tmp_path, capsys):
    fp = tmp_path / "data.bin"
    fp.write_bytes(b"x" * 100)

    uploader = _make_uploader()

    with (
        patch.object(uploader, "_compute_md5", new_callable=AsyncMock, return_value="abc"),
        patch.object(uploader, "_exists_in_s3", new_callable=AsyncMock, return_value=True),
        patch.object(uploader, "upload_file", new_callable=AsyncMock) as mock_upload,
    ):
        ok = await uploader._process_file(fp, "data.bin")

    assert ok is True
    mock_upload.assert_not_called()


# ---------------------------------------------------------------------------
# upload_folder — dispatching and S3 key construction
# ---------------------------------------------------------------------------


async def test_upload_folder_all_success(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "b.bin").write_bytes(b"y" * 200)

    uploader = _make_uploader()
    with patch.object(uploader, "_process_file", new_callable=AsyncMock, return_value=True):
        ok = await uploader.upload_folder(tmp_path, None)

    assert ok is True


async def test_upload_folder_partial_failure(tmp_path):
    (tmp_path / "ok.bin").write_bytes(b"x" * 100)
    (tmp_path / "bad.bin").write_bytes(b"y" * 200)

    uploader = _make_uploader()
    results = {"ok.bin": True, "bad.bin": False}

    async def _fake_process(path: Path, key: str) -> bool:
        return results[path.name]

    with patch.object(uploader, "_process_file", side_effect=_fake_process):
        ok = await uploader.upload_folder(tmp_path, None)

    assert ok is False


async def test_upload_folder_empty(tmp_path, capsys):
    uploader = _make_uploader()
    ok = await uploader.upload_folder(tmp_path, None)
    assert ok is True
    assert "No files" in capsys.readouterr().out


async def test_upload_folder_uses_s3_dest_prefix(tmp_path):
    (tmp_path / "file.bin").write_bytes(b"data")

    uploader = _make_uploader()
    captured_keys: list = []

    async def _fake_process(path: Path, key: str) -> bool:
        captured_keys.append(key)
        return True

    with patch.object(uploader, "_process_file", side_effect=_fake_process):
        await uploader.upload_folder(tmp_path, "archive/2026")

    assert captured_keys == ["archive/2026/file.bin"]


async def test_upload_folder_recurses_subdirectories(tmp_path):
    (tmp_path / "root.bin").write_bytes(b"r")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.bin").write_bytes(b"d")
    nested = sub / "nested"
    nested.mkdir()
    (nested / "bottom.bin").write_bytes(b"b")

    uploader = _make_uploader()
    captured_keys: list = []

    async def _fake_process(path: Path, key: str) -> bool:
        captured_keys.append(key)
        return True

    with patch.object(uploader, "_process_file", side_effect=_fake_process):
        ok = await uploader.upload_folder(tmp_path, None)

    assert ok is True
    assert sorted(captured_keys) == ["root.bin", "sub/deep.bin", "sub/nested/bottom.bin"]


async def test_upload_folder_recurse_preserves_prefix(tmp_path):
    sub = tmp_path / "photos" / "2026"
    sub.mkdir(parents=True)
    (sub / "img.jpg").write_bytes(b"img")

    uploader = _make_uploader()
    captured_keys: list = []

    async def _fake_process(path: Path, key: str) -> bool:
        captured_keys.append(key)
        return True

    with patch.object(uploader, "_process_file", side_effect=_fake_process):
        await uploader.upload_folder(tmp_path, "backup")

    assert captured_keys == ["backup/photos/2026/img.jpg"]
