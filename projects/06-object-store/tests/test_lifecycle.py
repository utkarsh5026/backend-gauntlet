"""Lifecycle: expiry, cold-tier migration, and the shared-blob age rule."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from object_store.bucket import BucketMetadata
from object_store.errors import InvalidRequest
from object_store.index import Index, NewVersion
from object_store.lifecycle import (
    Encoding,
    Lifecycle,
    LifecyclePolicy,
    LifecycleRule,
)
from object_store.multipart import Multipart
from object_store.naming import Bucket, Key
from object_store.objects import Digest, ETag, utc_now
from object_store.store import Store

BUCKET = Bucket("archive")


async def put(index: Index, key: str, data: bytes, store: Store) -> Digest:
    digest = await store.commit_bytes(data)
    await index.put(
        BUCKET,
        Key(key),
        NewVersion(
            digest=digest,
            etag=ETag(hashlib.md5(data).hexdigest()),
            size=len(data),
            content_type="application/octet-stream",
        ),
    )
    return digest


async def set_policy(index: Index, *rules: LifecycleRule) -> None:
    metadata = await index.load_bucket_metadata(BUCKET)
    metadata.lifecycle = LifecyclePolicy(rules=list(rules))
    await index.store_bucket_metadata(BUCKET, metadata)


# ── policy validation ───────────────────────────────────────────────────────


def test_first_matching_rule_wins() -> None:
    """Order matters: a specific rule must be able to precede a catch-all."""
    policy = LifecyclePolicy(
        rules=[
            LifecycleRule(id="logs", prefix="logs/", expire_after_days=7),
            LifecycleRule(id="everything", expire_after_days=365),
        ]
    )

    specific = policy.matching_rule("logs/app.log")
    catch_all = policy.matching_rule("photos/a.jpg")

    assert specific is not None and specific.id == "logs"
    assert catch_all is not None and catch_all.id == "everything"


def test_a_disabled_rule_is_skipped_but_kept() -> None:
    policy = LifecyclePolicy(rules=[LifecycleRule(id="paused", enabled=False, expire_after_days=1)])
    assert policy.matching_rule("anything") is None
    assert len(policy.rules) == 1


def test_tiering_after_expiry_is_incoherent() -> None:
    """You cannot cool a thing you have already deleted."""
    policy = LifecyclePolicy(rules=[LifecycleRule(tier_after_days=30, expire_after_days=30)])
    with pytest.raises(InvalidRequest):
        policy.validate_coherent()


def test_a_zero_day_age_is_rejected() -> None:
    with pytest.raises(InvalidRequest):
        LifecyclePolicy(rules=[LifecycleRule(expire_after_days=0)]).validate_coherent()


def test_a_coherent_policy_validates() -> None:
    LifecyclePolicy(
        rules=[LifecycleRule(tier_after_days=30, expire_after_days=365)]
    ).validate_coherent()


# ── the sweep ───────────────────────────────────────────────────────────────


async def test_expiry_removes_an_aged_object(
    index: Index, store: Store, lifecycle: Lifecycle
) -> None:
    await index.create_bucket(BUCKET)
    await put(index, "old.log", b"stale", store)
    await set_policy(index, LifecycleRule(expire_after_days=30))

    report = await lifecycle.run_once_at(utc_now() + timedelta(days=31))

    assert report.expired == 1
    assert await index.get(BUCKET, Key("old.log")) is None


async def test_expiry_spares_a_young_object(
    index: Index, store: Store, lifecycle: Lifecycle
) -> None:
    await index.create_bucket(BUCKET)
    await put(index, "fresh.log", b"new", store)
    await set_policy(index, LifecycleRule(expire_after_days=30))

    report = await lifecycle.run_once_at(utc_now() + timedelta(days=29))

    assert report.expired == 0
    assert await index.get(BUCKET, Key("fresh.log")) is not None


async def test_a_prefix_filter_scopes_the_rule(
    index: Index, store: Store, lifecycle: Lifecycle
) -> None:
    await index.create_bucket(BUCKET)
    await put(index, "logs/a.log", b"log", store)
    await put(index, "photos/a.jpg", b"photo", store)
    await set_policy(index, LifecycleRule(prefix="logs/", expire_after_days=1))

    await lifecycle.run_once_at(utc_now() + timedelta(days=2))

    assert await index.get(BUCKET, Key("logs/a.log")) is None
    assert await index.get(BUCKET, Key("photos/a.jpg")) is not None


async def test_an_expired_object_becomes_gc_able(
    index: Index, store: Store, lifecycle: Lifecycle
) -> None:
    """Expiry drops the pointer; GC is still what reclaims the bytes."""
    await index.create_bucket(BUCKET)
    digest = await put(index, "doomed", b"bytes to reclaim", store)
    await set_policy(index, LifecycleRule(expire_after_days=1))

    await lifecycle.run_once_at(utc_now() + timedelta(days=2))
    assert await store.contains(digest)

    assert await index.gc() == 1
    assert not await store.contains(digest)


async def test_tiering_compresses_a_blob_and_keeps_it_readable(
    index: Index, store: Store, lifecycle: Lifecycle
) -> None:
    """Hash-then-compress: the identity does not move when the encoding does."""
    await index.create_bucket(BUCKET)
    payload = b"highly compressible " * 500
    digest = await put(index, "cold.bin", payload, store)
    await set_policy(index, LifecycleRule(tier_after_days=90))

    report = await lifecycle.run_once_at(utc_now() + timedelta(days=91))

    assert report.tiered == 1
    assert report.bytes_reclaimed > 0
    assert not store.blob_path(digest).exists()
    assert lifecycle.cold_path(digest).is_file()

    physical = await lifecycle.locate(digest)
    assert physical.encoding is Encoding.ZSTD

    reader = await lifecycle.open_tiered(digest)
    try:
        assert reader.read() == payload
    finally:
        reader.close()


async def test_tiering_is_idempotent(index: Index, store: Store, lifecycle: Lifecycle) -> None:
    await index.create_bucket(BUCKET)
    digest = await put(index, "cold.bin", b"data " * 200, store)
    await set_policy(index, LifecycleRule(tier_after_days=1))

    await lifecycle.run_once_at(utc_now() + timedelta(days=2))
    await lifecycle.tier_blob(digest)

    assert lifecycle.cold_path(digest).is_file()


async def backdate(index: Index, key: str, days: int) -> None:
    """Rewrite a row's `last_modified` to `days` ago, on disk.

    Editing the persisted row is what "an old object" actually means here —
    there is no other clock. Injecting a sweep instant far in the future is the
    usual trick, but it ages *every* key equally, which is exactly what the test
    below needs to avoid.
    """
    meta = await index.get(BUCKET, Key(key))
    assert meta is not None
    for version in meta.versions:
        version.last_modified = version.last_modified - timedelta(days=days)
    index.index_path(BUCKET, Key(key)).write_bytes(meta.model_dump_json().encode("utf-8"))


async def test_a_shared_blob_is_only_as_old_as_its_youngest_referrer(
    index: Index, store: Store, lifecycle: Lifecycle
) -> None:
    """The subtlety that bites: dedup means one blob backs many keys.

    An old key and a key written this morning can share bytes. Tiering on the
    old key's age alone would freeze an object that is being actively read, and
    the only symptom is a latency regression nobody can explain.
    """
    await index.create_bucket(BUCKET)
    payload = b"shared between an old key and a new one" * 20
    digest = await put(index, "ancient", payload, store)
    await backdate(index, "ancient", days=100)

    # A second key on the same bytes, written now.
    await index.put(
        BUCKET,
        Key("brand-new"),
        NewVersion(
            digest=digest,
            etag=ETag(hashlib.md5(payload).hexdigest()),
            size=len(payload),
            content_type="application/octet-stream",
        ),
    )
    await set_policy(index, LifecycleRule(tier_after_days=30))

    # "ancient" matches the rule and is 100 days old, but the blob is pinned hot
    # by "brand-new".
    assert (await lifecycle.run_once()).tiered == 0
    assert store.blob_path(digest).is_file()

    # Drop the young referrer and the same blob becomes eligible — which is what
    # proves the guard above was the youngest-referrer rule and not an accident.
    await index.delete(BUCKET, Key("brand-new"))
    assert (await lifecycle.run_once()).tiered == 1
    assert lifecycle.cold_path(digest).is_file()


async def test_expiry_runs_before_tiering(index: Index, store: Store, lifecycle: Lifecycle) -> None:
    """Otherwise the sweep pays to compress blobs it is about to delete."""
    await index.create_bucket(BUCKET)
    digest = await put(index, "doomed", b"about to go" * 100, store)
    await set_policy(index, LifecycleRule(tier_after_days=10, expire_after_days=20))

    report = await lifecycle.run_once_at(utc_now() + timedelta(days=21))

    assert report.expired == 1
    assert report.tiered == 0
    assert not lifecycle.cold_path(digest).exists()


async def test_noncurrent_versions_expire_without_touching_the_live_one(
    index: Index, store: Store, lifecycle: Lifecycle
) -> None:
    await index.create_bucket(BUCKET)
    await put(index, "doc", b"v1", store)
    await put(index, "doc", b"v2", store)
    await set_policy(index, LifecycleRule(noncurrent_expire_after_days=7))

    report = await lifecycle.run_once_at(utc_now() + timedelta(days=8))

    assert report.noncurrent_expired == 1
    meta = await index.get(BUCKET, Key("doc"))
    assert meta is not None
    assert [v.id for v in meta.versions] == [2]


async def test_stale_multipart_sessions_are_aborted(
    index: Index, store: Store, lifecycle: Lifecycle, multipart: Multipart
) -> None:
    """Staged parts are the one storage GC cannot see — no key references them."""
    await index.create_bucket(BUCKET)
    upload_id = await multipart.initiate(BUCKET, Key("abandoned"), "text/plain")
    await set_policy(index, LifecycleRule(abort_multipart_after_days=7))

    report = await lifecycle.run_once_at(utc_now() + timedelta(days=8))

    assert report.uploads_aborted == 1
    assert not multipart.staging_dir(upload_id).exists()


async def test_a_bucket_with_no_rules_is_untouched(
    index: Index, store: Store, lifecycle: Lifecycle
) -> None:
    await index.create_bucket(BUCKET)
    await put(index, "kept", b"forever", store)

    report = await lifecycle.run_once_at(utc_now() + timedelta(days=3650))

    assert report.expired == 0
    assert report.tiered == 0


async def test_a_future_last_modified_never_counts_as_old(
    index: Index, store: Store, lifecycle: Lifecycle
) -> None:
    """Clock skew happens, and the action here is deletion."""
    await index.create_bucket(BUCKET)
    await put(index, "skewed", b"data", store)
    await set_policy(index, LifecycleRule(expire_after_days=1))

    report = await lifecycle.run_once_at(utc_now() - timedelta(days=365))

    assert report.expired == 0


async def test_bucket_metadata_defaults_when_the_file_is_absent(
    index: Index, data_dir: Path
) -> None:
    """A bucket created before the document existed must still load."""
    await index.create_bucket(BUCKET)
    BucketMetadata.path_in(index.bucket_dir(BUCKET)).unlink()

    metadata = await index.load_bucket_metadata(BUCKET)
    assert metadata.lifecycle.rules == []
