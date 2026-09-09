"""Property tests — the invariants, attacked with generated inputs.

Example-based tests check the cases you thought of. These check the *laws*, over
inputs nobody chose: naming safety, digest independence from framing, the
listing algebra, and the multipart ETag formula.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from object_store.cdc import CdcChunker
from object_store.config import CdcSettings
from object_store.multipart import multipart_etag
from object_store.naming import encode_key
from object_store.objects import Digest, ETag, ObjectMeta
from object_store.store import Store
from object_store.streaming import stream_to_store

KEYS = st.text(min_size=1, max_size=200).filter(lambda k: len(k.encode("utf-8")) <= 1024)
BLOBS = st.binary(min_size=0, max_size=4096)

SLOW = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ── naming ──────────────────────────────────────────────────────────────────


@given(KEYS)
def test_an_encoded_key_is_always_one_safe_filename(key: str) -> None:
    """No separators, no traversal, pure ASCII — for every key S3 accepts."""
    encoded = encode_key(key)

    assert "/" not in encoded
    assert "\\" not in encoded
    assert "\x00" not in encoded
    assert encoded.isascii()
    assert encoded not in {".", ".."}


@given(KEYS, KEYS)
def test_encoding_is_injective(left: str, right: str) -> None:
    """Two distinct keys can never land on the same index filename."""
    if left != right:
        assert encode_key(left) != encode_key(right)


# ── content addressing ──────────────────────────────────────────────────────


@given(BLOBS)
def test_a_digest_is_a_function_of_the_bytes_alone(data: bytes) -> None:
    assert Digest.of(data) == Digest.of(data)
    assert Digest.of(data) == hashlib.sha256(data).hexdigest()
    assert len(Digest.of(data)) == Digest.LEN


@given(st.lists(st.binary(min_size=1, max_size=200), min_size=1, max_size=20))
@SLOW
def test_framing_never_changes_the_digest(chunks: list[bytes]) -> None:
    """A digest is a property of the content, not of how the socket split it."""
    joined = b"".join(chunks)
    running = hashlib.sha256()
    for chunk in chunks:
        running.update(chunk)

    assert running.hexdigest() == hashlib.sha256(joined).hexdigest()


@given(st.lists(st.binary(min_size=1, max_size=512), min_size=1, max_size=8))
@SLOW
async def test_streaming_any_framing_stores_the_same_blob(
    store: Store, chunks: list[bytes]
) -> None:
    async def frames(pieces: list[bytes]) -> AsyncIterator[bytes]:
        for piece in pieces:
            yield piece

    joined = b"".join(chunks)
    many = await stream_to_store(store, frames(chunks), 1_000_000)
    one = await stream_to_store(store, frames([joined]), 1_000_000)

    assert many.digest == one.digest
    assert many.etag == one.etag
    assert many.size == one.size == len(joined)


# ── the multipart ETag ──────────────────────────────────────────────────────


@given(st.lists(st.binary(min_size=1, max_size=256), min_size=1, max_size=12))
def test_the_multipart_etag_matches_the_formula(parts: list[bytes]) -> None:
    raw = [hashlib.md5(part).digest() for part in parts]
    expected = f"{hashlib.md5(b''.join(raw)).hexdigest()}-{len(parts)}"

    assert multipart_etag(raw) == expected


@given(st.lists(st.binary(min_size=1, max_size=64), min_size=2, max_size=8))
def test_reordering_the_parts_changes_the_etag(parts: list[bytes]) -> None:
    """Order is load-bearing: assembling out of order corrupts the object."""
    raw = [hashlib.md5(part).digest() for part in parts]
    reversed_raw = list(reversed(raw))

    if raw != reversed_raw:
        assert multipart_etag(raw) != multipart_etag(reversed_raw)


@given(st.lists(st.binary(min_size=1, max_size=64), min_size=1, max_size=8))
def test_the_etag_suffix_is_always_the_part_count(parts: list[bytes]) -> None:
    raw = [hashlib.md5(part).digest() for part in parts]
    assert multipart_etag(raw).endswith(f"-{len(parts)}")


# ── the version history ─────────────────────────────────────────────────────


@given(st.lists(BLOBS, min_size=1, max_size=10))
def test_version_ids_climb_and_are_unique(payloads: list[bytes]) -> None:
    from object_store.naming import Bucket, Key

    meta = ObjectMeta.new_live(
        Bucket("photos"),
        Key("k"),
        Digest.of(payloads[0]),
        ETag("e"),
        len(payloads[0]),
        "text/plain",
    )
    for payload in payloads[1:]:
        meta.append_live(Digest.of(payload), ETag("e"), len(payload), "text/plain")

    ids = [version.id for version in meta.versions]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert meta.latest == ids[-1]


@given(st.lists(BLOBS, min_size=2, max_size=8))
def test_removing_a_version_never_frees_its_id(payloads: list[bytes]) -> None:
    """The contract that makes `?versionId=` safe to hold onto."""
    from object_store.naming import Bucket, Key

    meta = ObjectMeta.new_live(
        Bucket("photos"), Key("k"), Digest.of(payloads[0]), ETag("e"), 1, "text/plain"
    )
    for payload in payloads[1:]:
        meta.append_live(Digest.of(payload), ETag("e"), 1, "text/plain")

    used = {version.id for version in meta.versions}
    newest = max(used)
    meta.remove_version(newest)
    fresh = meta.append_live(Digest.of(b"new"), ETag("e"), 1, "text/plain")

    assert fresh not in used


# ── chunking ────────────────────────────────────────────────────────────────


CDC = CdcSettings(
    _env_file=None,  # type: ignore[call-arg]
    enabled=True,
    min_chunk=64,
    avg_chunk=256,
    max_chunk=1024,
    min_object=0,
)


@given(st.binary(min_size=0, max_size=20_000))
@SLOW
def test_chunking_is_lossless(data: bytes) -> None:
    chunker = CdcChunker(CDC)
    chunks = chunker.push(data) + chunker.finish()
    assert b"".join(chunks) == data


@given(st.binary(min_size=2048, max_size=20_000))
@SLOW
def test_chunks_respect_the_maximum(data: bytes) -> None:
    """The forced cut is what bounds memory on adversarial input."""
    chunker = CdcChunker(CDC)
    for chunk in chunker.push(data) + chunker.finish():
        assert len(chunk) <= CDC.max_chunk


# ── ranges ──────────────────────────────────────────────────────────────────


@given(
    st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=10),
    st.integers(min_value=0),
    st.integers(min_value=0),
)
def test_map_range_covers_exactly_the_request(
    sizes: list[int], start_seed: int, span_seed: int
) -> None:
    """Every logical byte asked for appears once, in order."""
    from object_store.manifest import ChunkRef, Manifest, map_range

    manifest = Manifest.new(
        [ChunkRef.of(bytes([index % 256]) * size) for index, size in enumerate(sizes)]
    )
    total = manifest.logical_size
    start = start_seed % total
    end = min(start + span_seed % total, total - 1)

    slices = map_range(manifest, start, end)

    assert sum(chunk.length for chunk in slices) == end - start + 1
    for chunk in slices:
        assert chunk.offset >= 0
        assert chunk.length > 0
