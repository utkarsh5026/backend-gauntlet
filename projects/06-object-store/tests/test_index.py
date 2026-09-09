"""V3 — the key namespace: listing, versioning, preconditions, and GC."""

from __future__ import annotations

import hashlib

import pytest

from object_store.errors import (
    BucketAlreadyExists,
    NoSuchBucket,
    NoSuchKey,
    PreconditionFailed,
)
from object_store.index import Index, NewVersion, Precondition
from object_store.naming import Bucket, Key
from object_store.objects import Digest, ETag, ObjectRef
from object_store.store import Store

BUCKET = Bucket("photos")


def version_of(data: bytes, content_type: str = "text/plain") -> NewVersion:
    return NewVersion(
        digest=Digest(hashlib.sha256(data).hexdigest()),
        etag=ETag(hashlib.md5(data).hexdigest()),
        size=len(data),
        content_type=content_type,
    )


async def put_key(index: Index, key: str, data: bytes) -> None:
    await index.put(BUCKET, Key(key), version_of(data))


async def test_create_bucket_is_not_idempotent(index: Index) -> None:
    """S3 distinguishes "made it" from "it was already there" — so must we."""
    await index.create_bucket(BUCKET)
    with pytest.raises(BucketAlreadyExists):
        await index.create_bucket(BUCKET)


async def test_operations_on_a_missing_bucket_raise(index: Index) -> None:
    with pytest.raises(NoSuchBucket):
        await index.ensure_bucket(Bucket("absent"))


async def test_put_then_resolve_round_trips(index: Index) -> None:
    await index.create_bucket(BUCKET)
    data = b"hello world"
    await put_key(index, "greeting.txt", data)

    resolved = await index.resolve(BUCKET, Key("greeting.txt"))

    assert resolved.size == len(data)
    assert resolved.content_type == "text/plain"
    assert resolved.etag == hashlib.md5(data).hexdigest()


async def test_resolve_raises_for_a_missing_key(index: Index) -> None:
    await index.create_bucket(BUCKET)
    with pytest.raises(NoSuchKey):
        await index.resolve(BUCKET, Key("nothing-here"))


async def test_a_traversal_shaped_key_stays_inside_the_bucket(index: Index) -> None:
    """The keyspace guard: `../` in a key is characters, never a directory."""
    await index.create_bucket(BUCKET)
    key = Key("../../etc/passwd")
    await index.put(BUCKET, key, version_of(b"not your passwd"))

    path = index.index_path(BUCKET, key)
    assert path.parent == index.objects_dir(BUCKET)
    assert path.is_file()
    assert (await index.resolve(BUCKET, key)).size == 15


async def test_overwrite_appends_a_version_and_flips_latest(index: Index) -> None:
    await index.create_bucket(BUCKET)
    key = Key("mutable.txt")
    await index.put(BUCKET, key, version_of(b"first"))
    await index.put(BUCKET, key, version_of(b"second"))

    meta = await index.get(BUCKET, key)
    assert meta is not None
    assert len(meta.versions) == 2
    assert meta.latest == 2
    assert (await index.resolve(BUCKET, key)).size == len(b"second")
    # The previous version is still addressable by id.
    assert (await index.resolve(BUCKET, key, ObjectRef.version(1))).size == len(b"first")


async def test_version_ids_are_never_reused(index: Index) -> None:
    """Deleting the newest version must not rewind the counter.

    If it did, the next PUT would hand a client's saved `?versionId=2` a
    different object — silent, and undetectable from the outside.
    """
    await index.create_bucket(BUCKET)
    key = Key("versioned.txt")
    await index.put(BUCKET, key, version_of(b"one"))
    await index.put(BUCKET, key, version_of(b"two"))
    await index.delete(BUCKET, key, ObjectRef.version(2))
    await index.put(BUCKET, key, version_of(b"three"))

    meta = await index.get(BUCKET, key)
    assert meta is not None
    assert meta.latest == 3
    assert {v.id for v in meta.versions} == {1, 3}


async def test_delete_latest_is_idempotent(index: Index) -> None:
    """S3 returns 204 for a key that was never there; clients retry deletes."""
    await index.create_bucket(BUCKET)
    await index.delete(BUCKET, Key("never-existed"))


async def test_if_none_match_star_is_create_once(index: Index) -> None:
    await index.create_bucket(BUCKET)
    key = Key("once.txt")

    await index.put(BUCKET, key, version_of(b"winner"), Precondition.if_none_match_star())
    with pytest.raises(PreconditionFailed):
        await index.put(BUCKET, key, version_of(b"loser"), Precondition.if_none_match_star())

    assert (await index.resolve(BUCKET, key)).size == len(b"winner")


async def test_if_match_is_compare_and_swap(index: Index) -> None:
    await index.create_bucket(BUCKET)
    key = Key("cas.txt")
    await index.put(BUCKET, key, version_of(b"base"))
    current = (await index.resolve(BUCKET, key)).etag

    await index.put(BUCKET, key, version_of(b"next"), Precondition.if_match(current))

    with pytest.raises(PreconditionFailed):
        await index.put(BUCKET, key, version_of(b"stale"), Precondition.if_match(current))


async def test_if_match_on_an_absent_key_fails(index: Index) -> None:
    await index.create_bucket(BUCKET)
    with pytest.raises(PreconditionFailed):
        await index.put(
            BUCKET,
            Key("ghost"),
            version_of(b"x"),
            Precondition.if_match(ETag("deadbeef")),
        )


# ── listing: the folder illusion ────────────────────────────────────────────


async def _seed_tree(index: Index) -> None:
    await index.create_bucket(BUCKET)
    for key in [
        "a/b/c.jpg",
        "a/b/d.jpg",
        "a/e.jpg",
        "f.jpg",
        "z/y/x.jpg",
    ]:
        await put_key(index, key, key.encode())


async def test_list_returns_every_key_sorted_without_a_delimiter(
    index: Index,
) -> None:
    """No delimiter means no folders: the keyspace is genuinely flat."""
    await _seed_tree(index)
    listing = await index.list(BUCKET)

    assert [str(m.key) for m in listing.objects] == [
        "a/b/c.jpg",
        "a/b/d.jpg",
        "a/e.jpg",
        "f.jpg",
        "z/y/x.jpg",
    ]
    assert listing.common_prefixes == []


async def test_delimiter_rolls_keys_into_common_prefixes(index: Index) -> None:
    await _seed_tree(index)
    listing = await index.list(BUCKET, delimiter="/")

    assert [str(m.key) for m in listing.objects] == ["f.jpg"]
    assert listing.common_prefixes == ["a/", "z/"]


async def test_prefix_and_delimiter_descend_one_level(index: Index) -> None:
    """`prefix=a/` + `delimiter=/` is what a client calls "open the a/ folder"."""
    await _seed_tree(index)
    listing = await index.list(BUCKET, prefix="a/", delimiter="/")

    assert [str(m.key) for m in listing.objects] == ["a/e.jpg"]
    assert listing.common_prefixes == ["a/b/"]


async def test_prefix_filters_without_a_delimiter(index: Index) -> None:
    await _seed_tree(index)
    listing = await index.list(BUCKET, prefix="a/b/")

    assert [str(m.key) for m in listing.objects] == ["a/b/c.jpg", "a/b/d.jpg"]


async def test_pagination_walks_every_key_exactly_once(index: Index) -> None:
    """The whole point of the token: no key skipped, none repeated."""
    await _seed_tree(index)

    seen: list[str] = []
    token: str | None = None
    while True:
        page = await index.list(BUCKET, max_keys=2, continuation=token)
        seen.extend(str(meta.key) for meta in page.objects)
        token = page.next_continuation_token
        if token is None:
            break

    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 5


async def test_pagination_interleaves_objects_and_prefixes_in_one_order(
    index: Index,
) -> None:
    """S3 pages objects and folders as a single sorted sequence, not two lists."""
    await _seed_tree(index)

    first = await index.list(BUCKET, delimiter="/", max_keys=1)
    assert first.common_prefixes == ["a/"]
    assert first.objects == []
    assert first.next_continuation_token == "a/"

    second = await index.list(
        BUCKET, delimiter="/", max_keys=1, continuation=first.next_continuation_token
    )
    assert [str(m.key) for m in second.objects] == ["f.jpg"]


async def test_deleted_keys_disappear_from_listings(index: Index) -> None:
    await _seed_tree(index)
    await index.delete(BUCKET, Key("f.jpg"))

    listing = await index.list(BUCKET)
    assert "f.jpg" not in [str(m.key) for m in listing.objects]


# ── garbage collection ──────────────────────────────────────────────────────


async def test_gc_reclaims_an_unreferenced_blob(index: Index, store: Store) -> None:
    await index.create_bucket(BUCKET)
    data = b"garbage soon"
    digest = await store.commit_bytes(data)
    await index.put(BUCKET, Key("doomed"), version_of(data))
    await index.delete(BUCKET, Key("doomed"))

    assert await index.gc() == 1
    assert not await store.contains(digest)


async def test_gc_keeps_a_referenced_blob(index: Index, store: Store) -> None:
    await index.create_bucket(BUCKET)
    data = b"still referenced"
    digest = await store.commit_bytes(data)
    await index.put(BUCKET, Key("live"), version_of(data))

    assert await index.gc() == 0
    assert await store.contains(digest)


async def test_dedup_means_delete_does_not_drop_shared_bytes(index: Index, store: Store) -> None:
    """Two keys, one blob. Deleting one must not destroy the other's object."""
    await index.create_bucket(BUCKET)
    data = b"shared between two keys"
    digest = await store.commit_bytes(data)
    await index.put(BUCKET, Key("first"), version_of(data))
    await index.put(BUCKET, Key("second"), version_of(data))

    assert len(list(store.file_cas.iter_blob_files())) == 1

    await index.delete(BUCKET, Key("first"))
    assert await index.gc() == 0
    assert await store.contains(digest)

    await index.delete(BUCKET, Key("second"))
    assert await index.gc() == 1
    assert not await store.contains(digest)


async def test_gc_spares_a_blob_whose_index_row_is_still_staged(index: Index, store: Store) -> None:
    """The in-flight-PUT race: bytes committed, pointer not yet renamed.

    A collector that only reads committed rows sees an unreferenced blob and
    deletes an upload that is about to succeed. The tmp scan is what closes it,
    and with the grace window at zero in tests, it is the *only* thing that
    does.
    """
    await index.create_bucket(BUCKET)
    data = b"committed but not yet pointed at"
    digest = await store.commit_bytes(data)

    from object_store.objects import ObjectMeta

    staged = ObjectMeta.new_live(
        BUCKET,
        Key("in-flight"),
        digest,
        ETag(hashlib.md5(data).hexdigest()),
        len(data),
        "application/octet-stream",
    )
    tmp = index.tmp_dir(BUCKET)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "in-flight.json").write_bytes(staged.model_dump_json().encode("utf-8"))

    assert await index.gc() == 0
    assert await store.contains(digest)


async def test_gc_survives_a_corrupt_staged_row(index: Index, store: Store) -> None:
    """A half-written temp is the expected shape of a crash — not a wedge."""
    await index.create_bucket(BUCKET)
    tmp = index.tmp_dir(BUCKET)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "torn.json").write_bytes(b'{"bucket": "photos", "ke')

    assert await index.gc() == 0
