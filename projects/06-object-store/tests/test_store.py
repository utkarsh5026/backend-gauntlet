"""V1 — the content-addressed blob store: dedup, fan-out, and the atomic commit."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from object_store.durable import TempEntry, publish_temp
from object_store.errors import IntegrityError, InvalidRequest, NoSuchKey
from object_store.manifest import BlobReader
from object_store.objects import Digest
from object_store.store import BlobLocation, Store


def digest_of(data: bytes) -> Digest:
    return Digest(hashlib.sha256(data).hexdigest())


async def commit(store: Store, data: bytes) -> Digest:
    """Stage and commit `data` the way V2's stream loop would."""
    digest = digest_of(data)
    temp = store.tmp_dir / f"stage-{digest}"
    temp.write_bytes(data)
    await store.commit_temp(temp, digest)
    return digest


async def read_all(reader: BlobReader) -> bytes:
    try:
        return reader.read()
    finally:
        reader.close()


def test_open_creates_the_whole_layout(data_dir: Path, store: Store) -> None:
    """Both backends and both staging trees exist from boot, whatever the policy."""
    for name in ("objects", "volumes", "tmp", "quarantine"):
        assert (data_dir / name).is_dir()


def test_blob_path_fans_out_deterministically(data_dir: Path, store: Store) -> None:
    digest = digest_of(b"hello")
    path = store.blob_path(digest)

    assert path == data_dir / "objects" / digest[0:2] / digest[2:4] / digest
    assert store.blob_path(digest) == path
    assert store.digest_from_path(path) == digest


def test_digest_from_path_rejects_anything_not_in_the_layout(data_dir: Path, store: Store) -> None:
    """GC acts on what this returns, so a lenient parse would delete stray files."""
    objects = data_dir / "objects"
    assert store.digest_from_path(objects / "loose-file") is None
    assert store.digest_from_path(objects / "ab" / "cd" / "not-a-digest") is None
    assert store.digest_from_path(Path("/etc/passwd")) is None


async def test_contains_is_false_for_an_unknown_digest(store: Store) -> None:
    assert not await store.contains(digest_of(b"never stored"))


async def test_commit_makes_the_blob_readable_and_consumes_the_temp(
    store: Store,
) -> None:
    data = b"the quick brown fox"
    temp = store.tmp_dir / "upload-1"
    temp.write_bytes(data)
    digest = digest_of(data)

    await store.commit_temp(temp, digest)

    assert await store.contains(digest)
    assert not temp.exists()
    assert await read_all(await store.open_blob(digest)) == data


async def test_identical_bytes_dedup_to_one_blob(store: Store) -> None:
    """The payoff of content addressing: the same bytes cannot be stored twice."""
    data = b"shared content"
    first = await commit(store, data)
    second = await commit(store, data)

    assert first == second
    assert len(list(store.file_cas.iter_blob_files())) == 1


async def test_commit_is_idempotent_and_leaves_the_original_bytes(
    store: Store,
) -> None:
    """A dedup hit must drop the temp — not overwrite a blob that is already good."""
    data = b"already here"
    digest = await commit(store, data)
    blob_path = store.blob_path(digest)
    before = blob_path.stat().st_mtime_ns

    temp = store.tmp_dir / "second-attempt"
    temp.write_bytes(data)
    await store.commit_temp(temp, digest)

    assert not temp.exists()
    assert blob_path.stat().st_mtime_ns == before
    assert blob_path.read_bytes() == data


async def test_remove_is_idempotent(store: Store) -> None:
    digest = await commit(store, b"transient")
    await store.remove(digest)
    assert not await store.contains(digest)
    await store.remove(digest)


async def test_open_blob_range_serves_only_the_slice(store: Store) -> None:
    data = bytes(range(256)) * 8
    digest = await commit(store, data)

    assert await read_all(await store.open_blob_range(digest, 0, 9)) == data[0:10]
    assert await read_all(await store.open_blob_range(digest, 100, 199)) == data[100:200]
    tail_start = len(data) - 5
    assert (
        await read_all(await store.open_blob_range(digest, tail_start, len(data) - 1))
        == data[tail_start:]
    )


async def test_open_blob_range_rejects_impossible_ranges(store: Store) -> None:
    data = b"0123456789"
    digest = await commit(store, data)

    with pytest.raises(InvalidRequest):
        await store.open_blob_range(digest, 5, 4)
    with pytest.raises(InvalidRequest):
        await store.open_blob_range(digest, 0, len(data))


async def test_open_blob_raises_for_a_missing_digest(store: Store) -> None:
    with pytest.raises(NoSuchKey):
        await store.open_blob(digest_of(b"absent"))


async def test_publish_temp_is_all_or_nothing(store: Store) -> None:
    """The heart of V1: a destination never exists in a partial state.

    A crash *during* `publish_temp` can only be before or after the rename,
    because rename is atomic within a filesystem — so the observable outcomes
    are "absent" and "complete", never "truncated under the right name". This
    asserts the shape rather than actually killing the process; the crash test
    that pulls the plug for real lives in the bench harness.
    """
    data = b"x" * 4096
    digest = digest_of(data)
    destination = store.blob_path(digest)
    temp = store.tmp_dir / "half-written"
    temp.write_bytes(data)

    assert not destination.exists()
    publish_temp(temp, destination)
    assert destination.read_bytes() == data
    assert not temp.exists()


def test_temp_entry_unlinks_on_an_early_exit(store: Store) -> None:
    """Every unhappy path in the stream loop relies on this."""
    staged: Path | None = None
    with pytest.raises(RuntimeError):
        with TempEntry.unique_in(store.tmp_dir, "doomed") as temp:
            staged = temp.path
            staged.write_bytes(b"partial upload")
            raise RuntimeError("client disconnected")

    assert staged is not None
    assert not staged.exists()


def test_temp_entry_survives_after_disarm(store: Store) -> None:
    with TempEntry.unique_in(store.tmp_dir, "kept") as temp:
        path = temp.path
        path.write_bytes(b"published")
        temp.disarm()

    assert path.exists()
    path.unlink()


async def test_scrub_quarantines_a_flipped_byte(store: Store) -> None:
    """Bit rot is silent; content addressing is what makes it detectable.

    A corrupt blob opens cleanly and has the right length — the only thing that
    says it is wrong is that its bytes no longer hash to its name.
    """
    data = b"important data that must not rot" * 32
    digest = await commit(store, data)
    blob_path = store.blob_path(digest)

    corrupted = bytearray(blob_path.read_bytes())
    corrupted[10] ^= 0xFF
    blob_path.write_bytes(bytes(corrupted))

    examined = await store.scrub_once()

    assert examined == 1
    assert store.is_quarantined(digest)
    assert not blob_path.exists()
    assert (store.quarantine_dir / str(digest)).exists()

    with pytest.raises(IntegrityError):
        await store.open_blob(digest)


async def test_scrub_verifies_a_healthy_blob_without_touching_it(
    store: Store,
) -> None:
    data = b"healthy" * 100
    digest = await commit(store, data)

    assert await store.scrub_once() == 1
    assert not store.is_quarantined(digest)
    assert await read_all(await store.open_blob(digest)) == data


async def test_scrub_of_an_empty_store_examines_nothing(store: Store) -> None:
    """The signal that parks the scrubber instead of spinning."""
    assert await store.scrub_once() == 0


async def test_haystack_policy_packs_small_and_falls_back_for_large(
    haystack_store: Store,
) -> None:
    small = b"tiny"
    small_digest = await commit(haystack_store, small)
    assert haystack_store.location(small_digest) is BlobLocation.HAYSTACK
    assert not haystack_store.blob_path(small_digest).exists()

    # One byte past what a framed needle can fit, so placement falls back.
    large = b"L" * (haystack_store.haystack_max_volume_size + 1)
    large_digest = await commit(haystack_store, large)
    assert haystack_store.location(large_digest) is BlobLocation.FILE_CAS
    assert haystack_store.blob_path(large_digest).is_file()

    assert await read_all(await haystack_store.open_blob(small_digest)) == small
    assert await read_all(await haystack_store.open_blob(large_digest)) == large


async def test_haystack_needles_survive_a_reopen(data_dir: Path, haystack_store: Store) -> None:
    """Boot must reconstruct the map from the snapshot plus the log."""
    data = b"packed needle"
    digest = await commit(haystack_store, data)
    haystack_store.close()

    from object_store.config import BlobLayoutKind

    reopened = Store(data_dir, layout=BlobLayoutKind.HAYSTACK, max_volume_size=64 * 1024)
    assert reopened.location(digest) is BlobLocation.HAYSTACK
    assert await read_all(await reopened.open_blob(digest)) == data
    reopened.close()


async def test_haystack_range_reads_stay_inside_the_needle(
    haystack_store: Store,
) -> None:
    """A volume is one long file — an unbounded read would run into the next object."""
    first = b"A" * 100
    second = b"B" * 100
    first_digest = await commit(haystack_store, first)
    second_digest = await commit(haystack_store, second)

    assert await read_all(await haystack_store.open_blob(first_digest)) == first
    assert await read_all(await haystack_store.open_blob(second_digest)) == second
    assert await read_all(await haystack_store.open_blob_range(first_digest, 0, 9)) == b"A" * 10


async def test_haystack_compaction_reclaims_tombstones(
    haystack_store: Store,
) -> None:
    """Deletes are tombstones; compaction is what actually frees the bytes."""
    keep = await commit(haystack_store, b"keep-alive")
    dead = await commit(haystack_store, b"dead-weight")

    await haystack_store.remove(dead)
    assert haystack_store.location(dead) is None

    dropped = await haystack_store.compact_haystack()

    assert dead in dropped
    assert haystack_store.location(keep) is BlobLocation.HAYSTACK
    assert await read_all(await haystack_store.open_blob(keep)) == b"keep-alive"


async def test_compaction_does_not_clobber_a_concurrent_commit(
    haystack_store: Store,
) -> None:
    """Compaction patches dropped keys; it must never rebuild the whole map.

    A full rebuild snapshots the backends and writes the result back, which
    silently drops any digest committed while the copy was running.
    """
    keep = await commit(haystack_store, b"keep")
    dead = await commit(haystack_store, b"dead")
    await haystack_store.remove(dead)

    concurrent = digest_of(b"committed-during-compact")
    # Reaching into the locator map on purpose: this simulates a `commit_temp`
    # insert landing *during* the compaction copy, which is the exact race a
    # full-map rebuild would lose and which no public API can stage.
    haystack_store.record_location_for_test(concurrent, BlobLocation.HAYSTACK)

    await haystack_store.compact_haystack()

    assert haystack_store.location(keep) is BlobLocation.HAYSTACK
    assert haystack_store.location(concurrent) is BlobLocation.HAYSTACK
    assert haystack_store.location(dead) is None
