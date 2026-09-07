"""The index-as-a-service split: the same API, one process further away."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from object_store.errors import (
    BucketAlreadyExists,
    NoSuchBucket,
    NoSuchKey,
    PreconditionFailed,
)
from object_store.index import Index, NewVersion, Precondition
from object_store.index_backend import RemoteIndex
from object_store.index_server import create_index_app
from object_store.naming import Bucket, Key
from object_store.objects import ETag, ObjectRef
from object_store.store import Store

BUCKET = Bucket("remote")


def version_of(data: bytes) -> NewVersion:
    from object_store.objects import Digest

    return NewVersion(
        digest=Digest(hashlib.sha256(data).hexdigest()),
        etag=ETag(hashlib.md5(data).hexdigest()),
        size=len(data),
        content_type="text/plain",
    )


@pytest_asyncio.fixture
async def remote(index: Index) -> AsyncIterator[RemoteIndex]:
    """A `RemoteIndex` wired to the real service app over an in-process transport.

    No socket, but every call still goes through JSON encode, HTTP routing, JSON
    decode and the error mapping — which is where the split's bugs actually
    live.
    """
    app = create_index_app(index)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://index")
    backend = RemoteIndex("http://index", client=client)
    yield backend
    await backend.aclose()


async def test_bucket_lifecycle_over_the_wire(remote: RemoteIndex) -> None:
    await remote.create_bucket(BUCKET)
    assert str(BUCKET) in await remote.buckets()
    await remote.ensure_bucket(BUCKET)

    with pytest.raises(BucketAlreadyExists):
        await remote.create_bucket(BUCKET)


async def test_a_missing_bucket_maps_back_to_its_exception(
    remote: RemoteIndex,
) -> None:
    """A 404 carrying `NoSuchBucket` must arrive as `NoSuchBucket`.

    Losing the code and degrading everything to a generic error is the failure
    that makes the split deployment quietly different from the local one.
    """
    with pytest.raises(NoSuchBucket):
        await remote.ensure_bucket(Bucket("absent"))


async def test_put_get_resolve_delete_over_the_wire(remote: RemoteIndex) -> None:
    await remote.create_bucket(BUCKET)
    key = Key("a/b/c.txt")
    payload = b"remote payload"

    meta = await remote.put(BUCKET, key, version_of(payload))
    assert meta.key == key

    fetched = await remote.get(BUCKET, key)
    assert fetched is not None
    assert fetched.latest == 1

    resolved = await remote.resolve(BUCKET, key)
    assert resolved.size == len(payload)
    assert resolved.content_type == "text/plain"
    assert resolved.last_modified.tzinfo is not None

    await remote.delete(BUCKET, key)
    with pytest.raises(NoSuchKey):
        await remote.resolve(BUCKET, key)


async def test_a_key_with_slashes_survives_the_wire(remote: RemoteIndex) -> None:
    """The key is one opaque segment on the wire, not a path."""
    await remote.create_bucket(BUCKET)
    key = Key("deep/nested/path/with spaces.txt")

    await remote.put(BUCKET, key, version_of(b"x"))
    assert (await remote.resolve(BUCKET, key)).key == key


async def test_preconditions_cross_the_wire(remote: RemoteIndex) -> None:
    await remote.create_bucket(BUCKET)
    key = Key("guarded")

    await remote.put(BUCKET, key, version_of(b"first"), Precondition.if_none_match_star())
    with pytest.raises(PreconditionFailed):
        await remote.put(BUCKET, key, version_of(b"second"), Precondition.if_none_match_star())


async def test_listing_crosses_the_wire(remote: RemoteIndex) -> None:
    await remote.create_bucket(BUCKET)
    for key in ("a/b.txt", "a/c.txt", "top.txt"):
        await remote.put(BUCKET, Key(key), version_of(key.encode()))

    listing = await remote.list(BUCKET, delimiter="/")

    assert [str(m.key) for m in listing.objects] == ["top.txt"]
    assert listing.common_prefixes == ["a/"]


async def test_versions_cross_the_wire(remote: RemoteIndex) -> None:
    await remote.create_bucket(BUCKET)
    key = Key("versioned")
    await remote.put(BUCKET, key, version_of(b"v1"))
    await remote.put(BUCKET, key, version_of(b"v2"))

    assert (await remote.resolve(BUCKET, key, ObjectRef.version(1))).size == 2
    assert (await remote.resolve(BUCKET, key)).version_id == 2


async def test_bucket_metadata_crosses_the_wire(remote: RemoteIndex) -> None:
    from object_store.lifecycle import LifecyclePolicy, LifecycleRule

    await remote.create_bucket(BUCKET)
    metadata = await remote.load_bucket_metadata(BUCKET)
    metadata.lifecycle = LifecyclePolicy(rules=[LifecycleRule(id="cool", tier_after_days=30)])
    await remote.store_bucket_metadata(BUCKET, metadata)

    reloaded = await remote.load_bucket_metadata(BUCKET)
    assert reloaded.lifecycle.rules[0].id == "cool"
    assert reloaded.lifecycle.rules[0].tier_after_days == 30


async def test_gc_crosses_the_wire(remote: RemoteIndex, store: Store) -> None:
    await remote.create_bucket(BUCKET)
    data = b"collectable"
    digest = await store.commit_bytes(data)
    await remote.put(BUCKET, Key("doomed"), version_of(data))
    await remote.delete(BUCKET, Key("doomed"))

    assert await remote.gc() == 1
    assert not await store.contains(digest)


async def test_an_unreachable_index_service_is_an_error_not_a_hang() -> None:
    backend = RemoteIndex("http://127.0.0.1:1")
    from object_store.errors import AppError

    with pytest.raises(AppError, match="unreachable"):
        await backend.buckets()
    await backend.aclose()
