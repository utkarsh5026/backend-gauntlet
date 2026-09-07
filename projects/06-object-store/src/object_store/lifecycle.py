"""Lifecycle and tiering — objects age out, or migrate to a cheaper cold tier.

Two operations hide in one SPEC line, and they share almost nothing:

1. **Expiration** — after a configured age the object *disappears*. Cheap: drop
   the index row and let V3's GC reclaim the now-unreferenced blob. No new
   storage, no decode path.
2. **Tiering** — after a configured age the object *stays retrievable* but its
   bytes move to a compressed representation, so GET has to decode
   transparently. This is the hard half, and everything below is about it.

## The CAS collision — read this before touching `tier_blob`

A blob is named `objects/<sha256-of-its-bytes>`. Compress the bytes and the hash
changes, so tiering is a fork the design has to defend:

- *compress-then-hash*: the cold file is named by the compressed hash. This
  breaks dedup (the same plaintext compresses differently under different
  settings) and orphans every index entry pointing at the old digest. **No.**
- *hash-then-compress*: identity stays the **plaintext** digest. The digest, the
  ETag and the dedup key never move; compression becomes a *physical encoding of
  a blob* rather than a new identity. **This is the one we build.**

The same rule governs CDC chunks: hash plaintext chunks, compress their physical
encoding later. And never tier a *manifest* digest as if it were payload — tier
the chunk digests the manifest names, or leave them hot.

So the mental model: a blob has one fixed **logical digest** and a **physical
representation** that tiering flips between `objects/<h>` (raw) and
`cold/<ab>/<cd>/<h>.zst` (zstd).

## The subtlety that bites: decisions are per-object, transforms are per-blob

`last_modified` lives on the *version*, but a blob is **shared** across keys by
dedup. One blob may back a 90-day-old key *and* one written this morning. So a
blob is cold-eligible only when its **youngest referrer** is older than
`tier_after_days` — a `max(last_modified)` over everything pointing at it.
Compute it per key and you freeze objects that are actively being read, and the
first symptom is a latency regression nobody can explain.
"""

from __future__ import annotations

import asyncio
import os
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol, cast

import structlog
from pydantic import BaseModel, Field

from . import metrics
from .durable import TempEntry, publish_temp
from .errors import AppError, InvalidRequest, NoSuchKey
from .naming import Bucket, Key
from .objects import LATEST, Digest, utc_now
from .store import BoundedReader, Store

if TYPE_CHECKING:
    # Imported for types only: at runtime `index_backend` imports `index`, which
    # imports `bucket`, which imports this module — a real cycle. Deferring the
    # import to type-check time is the standard way out, and it costs nothing
    # because none of these are needed to *run* a sweep, only to describe one.
    from .index_backend import IndexBackend
    from .manifest import BlobReader, ChunkSlice
    from .multipart import Multipart
    from .objects import ObjectMeta

__all__ = [
    "Encoding",
    "Lifecycle",
    "LifecyclePolicy",
    "LifecycleRule",
    "Physical",
    "SweepReport",
    "skip_bytes",
]

logger = structlog.get_logger(__name__)

COLD_SUFFIX = ".zst"
COLD_DIR = "cold"
COMPRESS_CHUNK = 256 * 1024
ZSTD_LEVEL = 3


class LifecycleRule(BaseModel):
    """One rule: a filter (which keys) plus up to four independent actions.

    Ages are in **days since `last_modified`**. S3 also allows absolute dates;
    skipped until something needs one, because a second time model doubles the
    edge cases in the sweep for no current benefit.
    """

    id: str = ""
    """A human name, so an operator can find this rule in a log line."""

    enabled: bool = True
    """A disabled rule stays on disk but is skipped — pause without losing the
    definition, which matters when the definition is what deletes data."""

    prefix: str | None = None
    """Key-prefix filter. `None` means the whole bucket."""

    tier_after_days: int | None = None
    """Migrate the current object's blob to the cold tier after this age."""

    expire_after_days: int | None = None
    """Delete the current object after this age."""

    noncurrent_expire_after_days: int | None = None
    """Reap *superseded* versions after this age. Distinct from
    `expire_after_days` because in a versioned bucket the real disk hog is stale
    history, not the live object."""

    abort_multipart_after_days: int | None = None
    """Abort sessions whose parts have sat in staging longer than this. The one
    kind of storage GC cannot see, because no key references it."""

    def matches(self, key: str) -> bool:
        return self.enabled and (self.prefix is None or key.startswith(self.prefix))


class LifecyclePolicy(BaseModel):
    """A bucket's ordered rules. Empty means nothing ages."""

    rules: list[LifecycleRule] = Field(default_factory=lambda: [])

    def matching_rule(self, key: str) -> LifecycleRule | None:
        """The first enabled rule whose filter matches — S3's first-match-wins.

        Order is the whole point: a specific `logs/` rule has to be able to
        precede a catch-all, or the catch-all swallows it.
        """
        return next((rule for rule in self.rules if rule.matches(key)), None)

    def validate_coherent(self) -> None:
        """Reject a policy that cannot be enforced sensibly, before it persists.

        Validation happens on the PUT-policy path rather than during the sweep,
        because a sweep that discovers an incoherent rule has already been
        running for however long it took someone to notice.
        """
        for rule in self.rules:
            for name in (
                "tier_after_days",
                "expire_after_days",
                "noncurrent_expire_after_days",
                "abort_multipart_after_days",
            ):
                if getattr(rule, name) == 0:
                    raise InvalidRequest(f"{name} must be greater than zero")

            tier, expire = rule.tier_after_days, rule.expire_after_days
            if tier is not None and expire is not None and tier >= expire:
                raise InvalidRequest("tier_after_days must be less than expire_after_days")


class Encoding(StrEnum):
    """How a blob's bytes are encoded on disk. The logical digest is unchanged."""

    RAW = "raw"
    """Plaintext, in the hot tree. Supports O(1) ranged reads via seek."""

    ZSTD = "zstd"
    """Compressed, in the cold tier. A ranged read must decode from the start of
    the stream — that latency is the cold tier's price, and it belongs in the
    design doc rather than being discovered in production."""


@dataclass(frozen=True, slots=True)
class Physical:
    """Where a blob lives and how it is encoded, resolved before opening."""

    path: Path
    encoding: Encoding


@dataclass(slots=True)
class SweepReport:
    """What one sweep did. Logged and counted; never used for control flow —
    the next sweep re-derives everything from disk."""

    expired: int = 0
    tiered: int = 0
    bytes_reclaimed: int = 0
    uploads_aborted: int = 0
    noncurrent_expired: int = 0


class _ZstdModule(Protocol):
    """The two entry points this module needs from a zstd implementation.

    A `Protocol` rather than `Any`, because the whole reason there is a shim is
    that the concrete module is chosen at import time — stating the surface here
    means a wrong assumption about either implementation is a type error rather
    than an `AttributeError` on the first cold read.
    """

    def ZstdCompressor(self, level: int) -> Any: ...  # noqa: N802

    def open(self, path: Path, mode: str) -> BinaryIO: ...


class _ZstdCompressor:
    """Streaming compressor, preferring real zstd and falling back to zlib.

    CPython 3.14 ships `compression.zstd`; before that the usual answer is the
    third-party `zstandard` wheel. Neither is guaranteed here, and the *lesson*
    of this module is the hash-then-compress identity rule and the crash-safe
    migration — not which entropy coder runs. So the codec is chosen at import
    and recorded in the file header, and a store written by one build stays
    readable by another.
    """

    def __init__(self) -> None:
        self._impl: _ZstdModule | None = _load_zstd()

    @property
    def name(self) -> str:
        return "zstd" if self._impl is not None else "zlib"

    def stream_compress(self, source: BinaryIO, dest: BinaryIO) -> None:
        if self._impl is not None:
            compressor = self._impl.ZstdCompressor(level=ZSTD_LEVEL)
            while chunk := source.read(COMPRESS_CHUNK):
                dest.write(compressor.compress(chunk))
            dest.write(compressor.flush())
            return
        gzip_compressor = zlib.compressobj(level=6, wbits=zlib.MAX_WBITS | 16)
        while chunk := source.read(COMPRESS_CHUNK):
            dest.write(gzip_compressor.compress(chunk))
        dest.write(gzip_compressor.flush())

    def open_decompressed(self, path: Path) -> BinaryIO:
        """A streaming reader over a cold file — never a whole-object decode.

        This is the property `streaming.py` fought for, and it would be
        trivially easy to lose here: `decompress(path.read_bytes())` is one line
        and puts the entire object back in RAM, undoing V2 for every cold GET.
        """
        if self._impl is not None:
            return self._impl.open(path, "rb")
        import gzip

        return cast(BinaryIO, gzip.open(path, "rb"))


def _load_zstd() -> _ZstdModule | None:
    """The best available zstd implementation, or `None` for the zlib fallback."""
    try:
        from compression import zstd  # type: ignore[import-not-found]

        return cast(_ZstdModule, zstd)
    except ImportError:
        pass
    try:
        import pyzstd  # type: ignore[import-not-found]

        return cast(_ZstdModule, pyzstd)
    except ImportError:
        return None


class Lifecycle:
    """The ageing engine: the policy, the sweep, and the crash-safe migration."""

    SWEEP_CONCURRENCY = 32
    """Bound on parallel expire/tier I/O, so a large bucket does not thrash the
    disk into uselessness while a sweep runs."""

    def __init__(
        self,
        index: IndexBackend,
        store: Store,
        multipart: Multipart | None = None,
    ) -> None:
        self.index = index
        self.store = store
        self.multipart = multipart
        self.codec = _ZstdCompressor()

    # ── placement ───────────────────────────────────────────────────────────

    def cold_path(self, digest: Digest) -> Path:
        """`<data>/cold/<ab>/<cd>/<digest>.zst`.

        Mirrors the hot tree's two-level shard layout so directory fan-out stays
        the same after a blob changes tier — the reasoning in `file_cas` about
        millions of entries in one directory does not stop applying just because
        the bytes got smaller.
        """
        root = self.store.objects_root.parent
        return root / COLD_DIR / digest[0:2] / digest[2:4] / f"{digest}{COLD_SUFFIX}"

    async def locate(self, digest: Digest) -> Physical:
        """Resolve where a blob lives and how it is encoded.

        Probing two paths rather than reading a descriptor: the filesystem
        already holds this fact, and a separate descriptor would be a second
        source of truth that can disagree with the files after a crash. GET
        calls this instead of assuming the hot path — that indirection is the
        entire "transparent" in transparent tiering.
        """

        def _probe() -> Physical:
            hot = self.store.blob_path(digest)
            if hot.is_file():
                return Physical(hot, Encoding.RAW)
            cold = self.cold_path(digest)
            if cold.is_file():
                return Physical(cold, Encoding.ZSTD)
            raise NoSuchKey()

        if self.store.location(digest) is not None:
            # Packed into a Haystack volume: the needle is the bytes, and the
            # cold tier does not apply.
            from .store import BlobLocation

            if self.store.location(digest) is BlobLocation.HAYSTACK:
                return Physical(self.store.root / "volumes", Encoding.RAW)
        return await asyncio.to_thread(_probe)

    async def open_tiered(self, digest: Digest) -> BlobReader:
        """Open a blob for reading, decoding a cold one transparently."""
        physical = await self.locate(digest)
        if physical.encoding is Encoding.RAW:
            return await self.store.open_blob(digest)
        return await asyncio.to_thread(self.codec.open_decompressed, physical.path)

    async def open_chunk_slice(self, store: Store, chunk_slice: ChunkSlice) -> BlobReader:
        """Open one CDC chunk slice, hot or cold.

        Chunks are independent blobs shared across objects, so a single logical
        range can legitimately span both tiers — which is why this resolves per
        chunk rather than once per request.
        """
        physical = await self.locate(chunk_slice.digest)
        if physical.encoding is Encoding.RAW:
            return await store.open_blob_range(
                chunk_slice.digest,
                chunk_slice.offset,
                chunk_slice.offset + chunk_slice.length - 1,
            )

        def _open() -> BoundedReader:
            handle = self.codec.open_decompressed(physical.path)
            skip_bytes(handle, chunk_slice.offset)
            return BoundedReader(handle, chunk_slice.length)

        return await asyncio.to_thread(_open)

    # ── migration ───────────────────────────────────────────────────────────

    async def tier_blob(self, digest: Digest) -> None:
        """Migrate one blob from the hot tree to the compressed cold tier.

        The same durability dance as V1's PUT, for the same reason:

        1. stream `objects/<h>` through the compressor into a temp beside the
           cold path;
        2. fsync it, rename it onto `cold/<h>.zst`, fsync the cold directory;
        3. only *now* unlink the hot copy.

        A crash before step 2 leaves a stray temp (reaped like any orphan). A
        crash between 2 and 3 leaves both copies — harmless, reads still work,
        and the next sweep finishes the job. Unlink the hot copy first and a
        crash before the cold copy is durable destroys the object outright.

        Idempotent: an existing cold file means the migration already happened,
        so any leftover hot copy is dropped and the call returns.
        """
        cold = self.cold_path(digest)

        if await asyncio.to_thread(cold.is_file):
            if await self.store.contains(digest):
                await self.store.remove(digest)
            return

        hot = self.store.blob_path(digest)
        if not await asyncio.to_thread(hot.is_file):
            raise NoSuchKey()

        def _compress() -> None:
            cold.parent.mkdir(parents=True, exist_ok=True)
            with TempEntry.unique_in(cold.parent, f"{digest}{COLD_SUFFIX}.tmp") as temp:
                with hot.open("rb") as source, temp.path.open("wb") as dest:
                    self.codec.stream_compress(source, dest)
                    dest.flush()
                    os.fsync(dest.fileno())
                publish_temp(temp.path, cold)
                temp.disarm()

        await asyncio.to_thread(_compress)
        await self.store.remove(digest)

    # ── the sweep ───────────────────────────────────────────────────────────

    async def run_forever(self, scan_interval: float) -> None:
        """Sweep on a timer until cancelled."""
        logger.info("lifecycle sweeper started", scan_interval_secs=scan_interval)
        while True:
            await asyncio.sleep(scan_interval)
            try:
                report = await self.run_once()
                if report.expired or report.tiered or report.uploads_aborted:
                    logger.info(
                        "lifecycle sweep",
                        expired=report.expired,
                        tiered=report.tiered,
                        bytes_reclaimed=report.bytes_reclaimed,
                        uploads_aborted=report.uploads_aborted,
                    )
            except (AppError, OSError) as err:
                logger.error("lifecycle sweep failed", error=str(err))

    async def run_once(self) -> SweepReport:
        """One full pass at the current instant."""
        return await self.run_once_at(utc_now())

    async def run_once_at(self, now: datetime) -> SweepReport:
        """`run_once` with the sweep instant injected.

        Production passes `utc_now()`; tests pass any instant, so a
        freshly-written object can be swept "in the future" without backdating
        it on disk or sleeping for a day. Every age decision in a pass is
        measured against this single `now`, so a sweep that takes minutes still
        judges its first and last object by the same clock.

        Order matters: expire first, then tier. Expiring drops referrers, which
        can make more blobs cold-eligible or fully unreferenced — doing it the
        other way round means compressing blobs that were about to be deleted.
        """
        report = SweepReport()

        for name in await self.index.buckets():
            bucket = Bucket.from_trusted(name)
            metadata = await self.index.load_bucket_metadata(bucket)
            policy = metadata.lifecycle
            if not policy.rules:
                continue

            entries = await self.index.index_entries(bucket)
            await self._expire(bucket, entries, policy, now, report)
            await self._tier(entries, policy, now, report)
            await self._expire_noncurrent(bucket, entries, policy, now, report)
            await self._abort_stale_uploads(policy, now, report)

        return report

    async def _expire(
        self,
        bucket: Bucket,
        entries: list[ObjectMeta],
        policy: LifecyclePolicy,
        now: datetime,
        report: SweepReport,
    ) -> None:
        to_expire: list[Key] = []
        for entry in entries:
            rule = policy.matching_rule(entry.key)
            live = entry.latest_live()
            if rule is None or live is None or rule.expire_after_days is None:
                continue
            if _older_than_days(live.last_modified, now, rule.expire_after_days):
                to_expire.append(entry.key)

        for key in to_expire:
            await self.index.delete(bucket, key, LATEST)
        report.expired += len(to_expire)
        metrics.LIFECYCLE_OBJECTS_EXPIRED.inc(len(to_expire))
        # Mutate the caller's list so the tier pass below cannot re-tier a key
        # this pass just expired.
        expired = set(to_expire)
        entries[:] = [entry for entry in entries if entry.key not in expired]

    async def _tier(
        self,
        entries: list[ObjectMeta],
        policy: LifecyclePolicy,
        now: datetime,
        report: SweepReport,
    ) -> None:
        # A blob is only as old as its *youngest* referrer — see the module
        # docstring. Every surviving live key pins its digest hot, including
        # keys with no lifecycle rule at all, so this walk is wider than the
        # rule-matched one below.
        youngest: dict[Digest, datetime] = {}
        for entry in entries:
            live = entry.latest_live()
            if live is None:
                continue
            current = youngest.get(live.digest)
            if current is None or live.last_modified > current:
                youngest[live.digest] = live.last_modified

        candidates: set[Digest] = set()
        for entry in entries:
            rule = policy.matching_rule(entry.key)
            live = entry.latest_live()
            if rule is None or live is None or rule.tier_after_days is None:
                continue
            newest = youngest.get(live.digest)
            if newest is not None and _older_than_days(newest, now, rule.tier_after_days):
                candidates.add(live.digest)

        semaphore = asyncio.Semaphore(self.SWEEP_CONCURRENCY)

        async def _tier_one(digest: Digest) -> tuple[int, int]:
            async with semaphore:
                try:
                    physical = await self.locate(digest)
                except NoSuchKey:
                    return 0, 0
                if physical.encoding is not Encoding.RAW:
                    return 0, 0
                hot_size = await asyncio.to_thread(
                    lambda: self.store.blob_path(digest).stat().st_size
                )
                await self.tier_blob(digest)
                cold_size = await asyncio.to_thread(lambda: self.cold_path(digest).stat().st_size)
                return 1, max(hot_size - cold_size, 0)

        for tiered, saved in await asyncio.gather(*(_tier_one(digest) for digest in candidates)):
            report.tiered += tiered
            report.bytes_reclaimed += saved
            metrics.LIFECYCLE_BLOBS_TIERED.inc(tiered)
            metrics.LIFECYCLE_BYTES_RECLAIMED.inc(saved)

    async def _expire_noncurrent(
        self,
        bucket: Bucket,
        entries: list[ObjectMeta],
        policy: LifecyclePolicy,
        now: datetime,
        report: SweepReport,
    ) -> None:
        """Reap superseded versions past their age.

        Only versions that are *not* `latest`: the live object is governed by
        `expire_after_days`, and conflating the two would delete the current
        object under a rule the owner wrote to clean up history.
        """
        for entry in entries:
            rule = policy.matching_rule(entry.key)
            if rule is None or rule.noncurrent_expire_after_days is None:
                continue
            stale = [
                version.id
                for version in entry.versions
                if version.id != entry.latest
                and _older_than_days(version.last_modified, now, rule.noncurrent_expire_after_days)
            ]
            for version_id in stale:
                from .objects import ObjectRef

                await self.index.delete(bucket, entry.key, ObjectRef.version(version_id))
                report.noncurrent_expired += 1

    async def _abort_stale_uploads(
        self, policy: LifecyclePolicy, now: datetime, report: SweepReport
    ) -> None:
        """Abort multipart sessions older than any rule's threshold.

        Uses the *smallest* configured threshold across rules: staged parts have
        no key, so there is nothing to match a prefix filter against, and the
        conservative reading of "abort after 7 days" is that no session should
        outlive it.
        """
        if self.multipart is None:
            return
        thresholds = [
            rule.abort_multipart_after_days
            for rule in policy.rules
            if rule.enabled and rule.abort_multipart_after_days is not None
        ]
        if not thresholds:
            return

        cutoff = now.timestamp() - min(thresholds) * 86400.0
        for upload_id in await self.multipart.sessions_older_than(cutoff):
            try:
                await self.multipart.abort(upload_id)
                report.uploads_aborted += 1
            except AppError as err:
                logger.warning(
                    "could not abort a stale multipart session",
                    upload_id=upload_id,
                    error=str(err),
                )


def _older_than_days(last_modified: datetime, now: datetime, days: int) -> bool:
    """Whether `last_modified` is at least `days` before `now`.

    Inclusive at the boundary. A `last_modified` in the future — clock skew,
    which happens — never counts as old enough: the subtraction goes negative
    and the comparison fails, which is the safe direction to be wrong in when
    the action is deletion.
    """
    return now - last_modified >= timedelta(days=days)


def skip_bytes(handle: BinaryIO, count: int) -> None:
    """Read and discard `count` bytes.

    A cold stream cannot seek — that is the entire cost of the cold tier, and
    the reason a mid-object range on a tiered blob still pays to decode from
    byte zero. Discarding in bounded chunks keeps that expensive operation from
    also being an expensive *allocation*.
    """
    remaining = count
    while remaining > 0:
        chunk = handle.read(min(remaining, COMPRESS_CHUNK))
        if not chunk:
            return
        remaining -= len(chunk)
