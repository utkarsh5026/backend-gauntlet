"""V2 — streaming: bounded memory, the size cap, and the unhappy paths."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator

import pytest

from object_store.errors import EntityTooLarge, InvalidRequest
from object_store.objects import BlobKind
from object_store.store import Store
from object_store.streaming import ChecksumSpec, stream_to_store


async def frames(*chunks: bytes) -> AsyncIterator[bytes]:
    """A body delivered as separate frames, the way a socket delivers one."""
    for chunk in chunks:
        yield chunk


async def failing_body(prefix: bytes) -> AsyncIterator[bytes]:
    """A body that dies mid-stream — a client disconnect."""
    yield prefix
    raise ConnectionResetError("client went away")


async def test_streams_a_multi_frame_body_into_one_blob(store: Store) -> None:
    stored = await stream_to_store(store, frames(b"hello ", b"world"), 1024)

    assert stored.size == 11
    assert stored.digest == hashlib.sha256(b"hello world").hexdigest()
    assert stored.etag == hashlib.md5(b"hello world").hexdigest()
    assert stored.blob_kind is BlobKind.WHOLE
    assert await store.contains(stored.digest)


async def test_frame_boundaries_do_not_change_the_result(store: Store) -> None:
    """The digest is a property of the bytes, not of how the network split them."""
    payload = b"the same bytes, chopped differently"
    one = await stream_to_store(store, frames(payload), 1024)
    many = await stream_to_store(
        store, frames(*[payload[i : i + 3] for i in range(0, len(payload), 3)]), 1024
    )

    assert one.digest == many.digest
    assert one.etag == many.etag


async def test_an_empty_body_is_a_legal_zero_byte_object(store: Store) -> None:
    stored = await stream_to_store(store, frames(), 1024)

    assert stored.size == 0
    assert stored.etag == hashlib.md5(b"").hexdigest()


async def test_the_cap_counts_the_running_total_not_one_frame(store: Store) -> None:
    """A body that dribbles past the cap in small pieces is still rejected."""
    with pytest.raises(EntityTooLarge):
        await stream_to_store(store, frames(b"a" * 6, b"b" * 6), max_size=10)


async def test_an_oversized_body_leaves_nothing_staged(store: Store) -> None:
    with pytest.raises(EntityTooLarge):
        await stream_to_store(store, frames(b"x" * 100), max_size=10)

    assert list(store.tmp_dir.iterdir()) == []
    assert list(store.file_cas.iter_blob_files()) == []


async def test_a_disconnect_mid_upload_leaves_nothing_staged(store: Store) -> None:
    """The unhappy path is the point — a dropped connection must not leak a temp."""
    with pytest.raises(ConnectionResetError):
        await stream_to_store(store, failing_body(b"partial upload"), 1_000_000)

    assert list(store.tmp_dir.iterdir()) == []
    assert list(store.file_cas.iter_blob_files()) == []


async def test_two_uploads_of_the_same_bytes_dedup(store: Store) -> None:
    first = await stream_to_store(store, frames(b"identical"), 1024)
    second = await stream_to_store(store, frames(b"identical"), 1024)

    assert first.digest == second.digest
    assert len(list(store.file_cas.iter_blob_files())) == 1


# ── client-declared checksums ───────────────────────────────────────────────


async def test_a_matching_content_md5_is_accepted(store: Store) -> None:
    payload = b"verified payload"
    spec = ChecksumSpec.from_headers(
        {"content-md5": base64.b64encode(hashlib.md5(payload).digest()).decode()}
    )
    assert spec is not None

    stored = await stream_to_store(store, frames(payload), 1024, spec)
    assert stored.size == len(payload)


async def test_a_mismatched_checksum_is_rejected_and_stores_nothing(
    store: Store,
) -> None:
    """S3 calls this `BadDigest`. Nothing durable may exist afterwards."""
    payload = b"actual bytes"
    spec = ChecksumSpec.from_headers(
        {"content-md5": base64.b64encode(hashlib.md5(b"different").digest()).decode()}
    )
    assert spec is not None

    with pytest.raises(InvalidRequest):
        await stream_to_store(store, frames(payload), 1024, spec)

    assert list(store.file_cas.iter_blob_files()) == []
    assert list(store.tmp_dir.iterdir()) == []


async def test_the_sha256_checksum_header_pair_is_honoured(store: Store) -> None:
    payload = b"sha-checked"
    spec = ChecksumSpec.from_headers(
        {
            "x-amz-checksum-algorithm": "sha256",
            "x-amz-checksum-sha256": base64.b64encode(hashlib.sha256(payload).digest()).decode(),
        }
    )
    assert spec is not None
    assert (await stream_to_store(store, frames(payload), 1024, spec)).size == 11


def test_a_named_algorithm_without_its_value_header_is_a_client_error() -> None:
    with pytest.raises(InvalidRequest):
        ChecksumSpec.from_headers({"x-amz-checksum-algorithm": "sha256"})


def test_an_unknown_algorithm_is_a_client_error() -> None:
    with pytest.raises(InvalidRequest):
        ChecksumSpec.from_headers({"x-amz-checksum-algorithm": "crc32c"})


def test_no_checksum_headers_means_no_verification() -> None:
    assert ChecksumSpec.from_headers({}) is None
    assert ChecksumSpec.from_headers(None) is None


async def test_invalid_base64_is_a_client_error(store: Store) -> None:
    spec = ChecksumSpec.from_headers({"content-md5": "not!valid!base64"})
    assert spec is not None
    with pytest.raises(InvalidRequest):
        await stream_to_store(store, frames(b"payload"), 1024, spec)
