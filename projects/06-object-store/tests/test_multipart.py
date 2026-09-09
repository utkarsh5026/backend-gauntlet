"""V4 — multipart sessions and the S3 ETag formula."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from object_store.errors import EntityTooLarge, InvalidRequest, NoSuchUpload
from object_store.index import Index
from object_store.multipart import Multipart, PartETag, multipart_etag
from object_store.naming import Bucket, Key
from object_store.store import Store

BUCKET = Bucket("uploads")
KEY = Key("big.bin")


async def frames(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def start(index: Index, multipart: Multipart) -> str:
    await index.create_bucket(BUCKET)
    return await multipart.initiate(BUCKET, KEY, "application/octet-stream")


# ── the formula ─────────────────────────────────────────────────────────────


def test_multipart_etag_matches_the_s3_formula() -> None:
    """`md5(concat(raw part md5s))-N`, computed by hand against the function."""
    parts = [b"a" * 100, b"b" * 100, b"c" * 50]
    raw = [hashlib.md5(part).digest() for part in parts]
    expected = f"{hashlib.md5(b''.join(raw)).hexdigest()}-3"

    assert multipart_etag(raw) == expected


def test_the_etag_hashes_raw_digests_not_hex() -> None:
    """The classic way to get this wrong: it produces a plausible, invalid value."""
    raw = [hashlib.md5(b"one").digest(), hashlib.md5(b"two").digest()]
    hexed = b"".join(digest.hex().encode() for digest in raw)

    assert multipart_etag(raw) != f"{hashlib.md5(hexed).hexdigest()}-2"


def test_the_suffix_is_the_part_count() -> None:
    """The `-N` is how a client knows not to verify by re-hashing the object."""
    for count in (1, 2, 17):
        raw = [hashlib.md5(bytes([i])).digest() for i in range(count)]
        assert multipart_etag(raw).endswith(f"-{count}")


def test_a_multipart_etag_differs_from_the_single_put_etag() -> None:
    """One part is still multipart — the value is not `md5(bytes)`."""
    payload = b"one part only"
    raw = [hashlib.md5(payload).digest()]

    assert multipart_etag(raw) != hashlib.md5(payload).hexdigest()
    assert multipart_etag(raw).endswith("-1")


# ── the session ─────────────────────────────────────────────────────────────


async def test_a_full_round_trip_assembles_in_part_order(
    index: Index, multipart: Multipart, store: Store
) -> None:
    upload_id = await start(index, multipart)
    payloads = {1: b"first-", 2: b"second-", 3: b"third"}

    # Uploaded out of order, the way parallel clients actually do it.
    tags = {}
    for number in (3, 1, 2):
        tags[number] = await multipart.upload_part(
            upload_id, number, frames(payloads[number]), 1_000_000
        )

    meta = await multipart.complete(upload_id, [tags[3], tags[1], tags[2]])

    live = meta.latest_live()
    assert live is not None
    assert live.size == sum(len(p) for p in payloads.values())

    reader = await store.open_blob(live.digest)
    with reader:
        assert reader.read() == b"first-second-third"

    expected = multipart_etag([hashlib.md5(payloads[n]).digest() for n in (1, 2, 3)])
    assert live.etag == expected


async def test_re_uploading_a_part_overwrites_it(
    index: Index, multipart: Multipart, store: Store
) -> None:
    """That is the retry story: a failed part is simply sent again."""
    upload_id = await start(index, multipart)
    await multipart.upload_part(upload_id, 1, frames(b"WRONG"), 1_000_000)
    good = await multipart.upload_part(upload_id, 1, frames(b"right"), 1_000_000)

    meta = await multipart.complete(upload_id, [good])
    live = meta.latest_live()
    assert live is not None

    reader = await store.open_blob(live.digest)
    with reader:
        assert reader.read() == b"right"


async def test_a_stale_part_etag_is_rejected(index: Index, multipart: Multipart) -> None:
    """The client retried a part and is completing with the old ETag."""
    upload_id = await start(index, multipart)
    stale = await multipart.upload_part(upload_id, 1, frames(b"first try"), 1_000_000)
    await multipart.upload_part(upload_id, 1, frames(b"second try"), 1_000_000)

    with pytest.raises(InvalidRequest, match="etag mismatch"):
        await multipart.complete(upload_id, [stale])


async def test_completing_with_a_part_that_was_never_staged_fails(
    index: Index, multipart: Multipart
) -> None:
    upload_id = await start(index, multipart)
    real = await multipart.upload_part(upload_id, 1, frames(b"present"), 1_000_000)
    ghost = PartETag(2, real.etag)

    with pytest.raises(InvalidRequest, match="no staged part 2"):
        await multipart.complete(upload_id, [real, ghost])


async def test_an_empty_part_list_is_rejected(index: Index, multipart: Multipart) -> None:
    upload_id = await start(index, multipart)
    with pytest.raises(InvalidRequest, match="at least one part"):
        await multipart.complete(upload_id, [])


async def test_duplicate_part_numbers_are_rejected(index: Index, multipart: Multipart) -> None:
    upload_id = await start(index, multipart)
    part = await multipart.upload_part(upload_id, 1, frames(b"x"), 1_000_000)

    with pytest.raises(InvalidRequest, match="duplicate part numbers"):
        await multipart.complete(upload_id, [part, part])


async def test_part_numbers_outside_the_s3_range_are_rejected(
    index: Index, multipart: Multipart
) -> None:
    upload_id = await start(index, multipart)
    for number in (0, -1, 10_001):
        with pytest.raises(InvalidRequest):
            await multipart.upload_part(upload_id, number, frames(b"x"), 1_000_000)


async def test_an_oversized_part_leaves_nothing_staged(index: Index, multipart: Multipart) -> None:
    upload_id = await start(index, multipart)
    with pytest.raises(EntityTooLarge):
        await multipart.upload_part(upload_id, 1, frames(b"y" * 100), max_part_size=10)

    assert not multipart.part_path(upload_id, 1).exists()


async def test_an_unknown_upload_id_is_not_found(multipart: Multipart) -> None:
    for call in (
        multipart.upload_part("does-not-exist", 1, frames(b"x"), 1024),
        multipart.abort("does-not-exist"),
    ):
        with pytest.raises(NoSuchUpload):
            await call


async def test_abort_reclaims_the_staged_parts(index: Index, multipart: Multipart) -> None:
    """The only way staged bytes are ever freed on the unhappy path."""
    upload_id = await start(index, multipart)
    await multipart.upload_part(upload_id, 1, frames(b"z" * 1000), 1_000_000)
    assert multipart.staging_dir(upload_id).is_dir()

    await multipart.abort(upload_id)

    assert not multipart.staging_dir(upload_id).exists()


async def test_complete_clears_the_staging_directory(index: Index, multipart: Multipart) -> None:
    upload_id = await start(index, multipart)
    part = await multipart.upload_part(upload_id, 1, frames(b"done"), 1_000_000)
    await multipart.complete(upload_id, [part])

    assert not multipart.staging_dir(upload_id).exists()


async def test_the_session_survives_a_restart_between_initiate_and_complete(
    data_dir: Path, index: Index, multipart: Multipart, store: Store
) -> None:
    """An upload can legitimately take hours; a deploy in the middle is normal."""
    from object_store.index_backend import LocalIndex

    upload_id = await start(index, multipart)
    part = await multipart.upload_part(upload_id, 1, frames(b"survives"), 1_000_000)

    restarted = Multipart(data_dir, store, LocalIndex(index))
    meta = await restarted.complete(upload_id, [part])

    assert meta.bucket == BUCKET
    assert meta.key == KEY
    assert meta.latest_live() is not None
