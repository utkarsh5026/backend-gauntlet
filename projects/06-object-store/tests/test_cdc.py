"""Content-defined chunking, and the manifests that reassemble it."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from object_store.cdc import CdcChunker, cut_point
from object_store.config import CdcSettings
from object_store.errors import InvalidRequest
from object_store.manifest import ChunkRef, Manifest, map_range
from object_store.store import Store
from object_store.streaming import stream_cdc_to_store

SETTINGS = CdcSettings(
    _env_file=None,  # type: ignore[call-arg]
    enabled=True,
    min_chunk=256,
    avg_chunk=1024,
    max_chunk=4096,
    min_object=0,
)


def sample(size: int, seed: int = 1234) -> bytes:
    """Deterministic pseudo-random bytes — random enough to have cut points."""
    import random

    return random.Random(seed).randbytes(size)


async def frames(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# ── the cutter ──────────────────────────────────────────────────────────────


def test_chunks_stay_inside_the_configured_band() -> None:
    chunker = CdcChunker(SETTINGS)
    chunks = chunker.push(sample(200_000)) + chunker.finish()

    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert SETTINGS.min_chunk <= len(chunk) <= SETTINGS.max_chunk
    assert len(chunks[-1]) <= SETTINGS.max_chunk


def test_the_chunks_reassemble_into_the_original() -> None:
    payload = sample(150_000)
    chunker = CdcChunker(SETTINGS)
    chunks = chunker.push(payload) + chunker.finish()

    assert b"".join(chunks) == payload


def test_frame_boundaries_do_not_change_the_chunks() -> None:
    """The property dedup depends on: a boundary is a fact about the bytes.

    Feed the same content as one frame or a thousand and the cuts land in the
    same places — otherwise two clients uploading identical data would share
    nothing.
    """
    payload = sample(120_000)

    single = CdcChunker(SETTINGS)
    one_shot = single.push(payload) + single.finish()

    dribbled = CdcChunker(SETTINGS)
    many: list[bytes] = []
    for offset in range(0, len(payload), 997):
        many.extend(dribbled.push(payload[offset : offset + 997]))
    many.extend(dribbled.finish())

    assert one_shot == many


def test_an_insertion_only_disturbs_nearby_chunks() -> None:
    """The whole reason CDC exists: fixed blocks would resync nowhere.

    Insert one byte near the front and the boundaries after it must re-align, so
    the two versions share most of their chunks.
    """
    original = sample(200_000)
    edited = original[:1000] + b"!" + original[1000:]

    def chunks_of(data: bytes) -> list[bytes]:
        chunker = CdcChunker(SETTINGS)
        return chunker.push(data) + chunker.finish()

    before = set(chunks_of(original))
    after = set(chunks_of(edited))
    shared = before & after

    assert len(shared) / len(before) > 0.8


def test_a_cut_point_is_never_below_the_floor() -> None:
    data = sample(10_000)
    end = cut_point(data, min_size=1024, avg_size=2048, max_size=8192)
    assert 1024 <= end <= 8192


def test_incoherent_settings_are_rejected() -> None:
    with pytest.raises(InvalidRequest):
        CdcChunker(
            CdcSettings(
                _env_file=None,  # type: ignore[call-arg]
                min_chunk=4096,
                avg_chunk=1024,
                max_chunk=256,
            )
        )


def test_an_empty_stream_yields_no_chunks() -> None:
    chunker = CdcChunker(SETTINGS)
    assert chunker.push(b"") == []
    assert chunker.finish() == []


# ── manifests ───────────────────────────────────────────────────────────────


def test_a_manifest_round_trips_through_its_bytes() -> None:
    manifest = Manifest.new([ChunkRef.of(b"one"), ChunkRef.of(b"two")])
    assert Manifest.from_bytes(manifest.to_bytes()) == manifest


def test_logical_size_is_the_sum_of_the_chunks() -> None:
    manifest = Manifest.new([ChunkRef.of(b"a" * 10), ChunkRef.of(b"b" * 25)])
    assert manifest.logical_size == 35


def test_map_range_covers_exactly_the_requested_bytes() -> None:
    manifest = Manifest.new(
        [ChunkRef.of(b"a" * 10), ChunkRef.of(b"b" * 10), ChunkRef.of(b"c" * 10)]
    )

    slices = map_range(manifest, 5, 24)

    assert [(s.offset, s.length) for s in slices] == [(5, 5), (0, 10), (0, 5)]
    assert sum(s.length for s in slices) == 20


def test_map_range_inside_one_chunk_touches_only_that_chunk() -> None:
    manifest = Manifest.new([ChunkRef.of(b"a" * 100), ChunkRef.of(b"b" * 100)])
    slices = map_range(manifest, 10, 19)

    assert len(slices) == 1
    assert (slices[0].offset, slices[0].length) == (10, 10)


def test_map_range_over_the_whole_object() -> None:
    manifest = Manifest.new([ChunkRef.of(b"x" * 7), ChunkRef.of(b"y" * 13)])
    slices = map_range(manifest, 0, 19)

    assert sum(s.length for s in slices) == 20


# ── the CDC PUT path ────────────────────────────────────────────────────────


async def test_a_cdc_put_stores_chunks_and_a_manifest(store: Store) -> None:
    import hashlib

    from object_store.objects import BlobKind

    payload = sample(80_000)
    stored = await stream_cdc_to_store(store, frames(payload), 1_000_000, None, SETTINGS)

    assert stored.blob_kind is BlobKind.MANIFEST
    assert stored.size == len(payload)
    # The ETag still describes the *logical* object — the client must not be
    # able to tell which storage strategy served it.
    assert stored.etag == hashlib.md5(payload).hexdigest()

    from object_store.manifest import load_manifest

    manifest = await load_manifest(store, stored.digest)
    assert manifest.logical_size == len(payload)
    assert len(manifest.chunks) > 1


async def test_two_similar_objects_share_most_of_their_chunks(
    store: Store,
) -> None:
    """The payoff whole-object dedup cannot deliver."""
    from object_store.manifest import load_manifest

    original = sample(200_000)
    edited = original[:5000] + b"CHANGED" + original[5000:]

    first = await stream_cdc_to_store(store, frames(original), 10_000_000, None, SETTINGS)
    blobs_after_first = len(list(store.file_cas.iter_blob_files()))

    second = await stream_cdc_to_store(store, frames(edited), 10_000_000, None, SETTINGS)
    blobs_after_second = len(list(store.file_cas.iter_blob_files()))

    assert first.digest != second.digest

    manifest_one = await load_manifest(store, first.digest)
    manifest_two = await load_manifest(store, second.digest)
    shared = {c.digest for c in manifest_one.chunks} & {c.digest for c in manifest_two.chunks}
    assert len(shared) / len(manifest_one.chunks) > 0.8

    # The second upload added only the changed chunks plus its own manifest.
    added = blobs_after_second - blobs_after_first
    assert added < len(manifest_two.chunks) // 2


async def test_the_cdc_size_cap_still_applies(store: Store) -> None:
    from object_store.errors import EntityTooLarge

    with pytest.raises(EntityTooLarge):
        await stream_cdc_to_store(store, frames(os.urandom(5000)), 1000, None, SETTINGS)
