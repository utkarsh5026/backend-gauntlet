"""V1 — the content-addressed blob store: the durable, dedup'd foundation.

This is the layer you would normally get from S3 or MinIO. Every distinct piece
of content lives exactly once, named by the SHA-256 of its bytes. V1 owns only
*"given finished bytes and their digest, store them safely and idempotently"* —
mapping `(bucket, key)` onto a digest is V3's job, and keeping those two honest
across a crash is the contract between them.

## The three ideas

**Content addressing.** The blob's filename is the hash of its bytes, so two
different keys holding identical content resolve to one file. Dedup is not a
feature here, it is a consequence of the naming scheme: there is no way to store
the same bytes twice.

**The atomic durable commit.** You cannot write straight to `objects/<hash>` — a
crash mid-write leaves a file with the *right name* and truncated contents, and
every future reader trusts it forever, because the name is supposed to be the
proof. The temp → fsync → rename → fsync-dir sequence in `durable` is the only
way to make "fully there or not there at all" true across power loss.

**Physical placement is owned here**, not by the index. Both backends are always
opened under the data dir — `FileCas` (one file per digest) and `Haystack`
(needles packed into volumes) — and a process-local locator map records which
one holds each digest, so GET, `contains` and `remove` never have to guess.
`BlobLayoutKind` only chooses the **write** policy, which is what makes it safe
to change: bytes already on disk keep being found either way.

## Async shape

The physical layers are synchronous (see `durable` on why `fsync` makes that
honest). Every method here that touches the disk hops to a thread with
`asyncio.to_thread`, so the event loop is never blocked by a `fsync` or a
multi-megabyte re-hash. That is the same design tokio's `fs` module has; making
it visible is the point.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

import structlog

from .. import metrics
from ..config import BlobLayoutKind
from ..durable import TempEntry
from ..errors import AppError, IntegrityError, InvalidRequest, NoSuchKey
from ..objects import Digest
from .file_cas import FileCas
from .haystack import Haystack, NeedleLocator, NeedleReader, VolumeId

__all__ = [
    "BlobLayoutKind",
    "BlobLocation",
    "BoundedReader",
    "FileCas",
    "Haystack",
    "NeedleLocator",
    "NeedleReader",
    "Store",
    "VolumeId",
]

logger = structlog.get_logger(__name__)

SCRUB_CHUNK = 1024 * 1024
"""Bytes per read while re-hashing. Big enough that syscall overhead is noise,
small enough that a scrub of a 5 GB blob never holds 5 GB of RAM."""


class BlobLocation(StrEnum):
    """Which backend holds a digest's bytes. The S3 index never sees this."""

    FILE_CAS = "file_cas"
    HAYSTACK = "haystack"


class BoundedReader:
    """A file handle that stops after N bytes, and closes itself when it does.

    Both backends need it for different reasons: Haystack because a needle is a
    slice of a shared volume, FileCas because a `Range` request wants a window
    of a file. Unifying them means the streaming GET path has exactly one reader
    protocol to care about.
    """

    __slots__ = ("_handle", "_remaining")

    def __init__(self, handle: BinaryIO, length: int) -> None:
        self._handle = handle
        self._remaining = length

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size < 0 else min(size, self._remaining)
        chunk = self._handle.read(want)
        self._remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> BoundedReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


BlobReader = BoundedReader | NeedleReader
"""What `open_blob` hands back: something with `read` and `close`."""


class Store:
    """Committed blobs, in-flight writes, and the integrity auditor over them."""

    def __init__(
        self,
        root: Path,
        *,
        layout: BlobLayoutKind = BlobLayoutKind.FILE_CAS,
        max_volume_size: int = 1024 * 1024,
    ) -> None:
        self.root = Path(root)
        self.policy = layout
        self.file_cas = FileCas(self.root)
        self.haystack = Haystack(self.root, max_volume_size)

        self.tmp_dir = self.root / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir = self.root / "quarantine"
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

        self._locations: dict[Digest, BlobLocation] = {}
        self._quarantined: set[Digest] = set()
        # Wakes the scrubber the moment a blob is committed, so a store that
        # was empty does not sit out a full rescan interval before auditing its
        # first object.
        self._scrub_wakeup = asyncio.Event()

        self._rebuild_locations()
        self._publish_occupancy_gauges()

    # ── boot ────────────────────────────────────────────────────────────────

    def _rebuild_locations(self) -> None:
        """Rebuild `digest → backend` from what is actually on disk.

        Boot and reopen only. Never call this after compaction: it replaces the
        whole map, which races a concurrent `commit_temp` insert and can drop a
        digest that was committed while the scan was running. `compact_haystack`
        patches the specific dropped keys instead.
        """
        locations: dict[Digest, BlobLocation] = {
            digest: BlobLocation.FILE_CAS for digest in self.file_cas.list_digests()
        }
        for digest in self.haystack.indexed_digests():
            if digest in locations:
                logger.warning(
                    "digest present in both FileCas and Haystack; preferring Haystack",
                    digest=str(digest),
                )
            locations[digest] = BlobLocation.HAYSTACK
        self._locations = locations

    def _publish_occupancy_gauges(self) -> None:
        _, file_bytes = self.file_cas.scan_occupancy()
        _, volume_bytes = self.haystack.scan_occupancy()
        metrics.BLOB_COUNT.set(len(self._locations))
        metrics.TOTAL_BYTES_STORED.set(file_bytes + volume_bytes)

    # ── placement ───────────────────────────────────────────────────────────

    @property
    def layout_kind(self) -> BlobLayoutKind:
        """The write policy chosen at open."""
        return self.policy

    def location(self, digest: Digest) -> BlobLocation | None:
        """Which backend currently holds `digest`, if it is committed at all."""
        return self._locations.get(digest)

    def _choose_location(self, payload_len: int) -> BlobLocation:
        if self.policy.packs_small and self.haystack.fits_in_volume(payload_len):
            return BlobLocation.HAYSTACK
        return BlobLocation.FILE_CAS

    @property
    def objects_root(self) -> Path:
        """Root of the FileCas blob tree — what GC and lifecycle walk."""
        return self.file_cas.objects_root

    def blob_path(self, digest: Digest) -> Path:
        """The FileCas path for a digest.

        Prefer `location` plus the open APIs for reads; this is for lifecycle
        and tooling that legitimately reason about the hot tree as a tree.
        """
        return self.file_cas.blob_path(digest)

    def digest_from_path(self, path: Path) -> Digest | None:
        return self.file_cas.digest_from_path(path)

    @property
    def haystack_max_volume_size(self) -> int:
        return self.haystack.max_volume_size

    # ── writes ──────────────────────────────────────────────────────────────

    async def contains(self, digest: Digest) -> bool:
        """Whether a committed blob exists for `digest` (locator map, no I/O)."""
        return digest in self._locations

    async def commit_temp(self, temp: Path, digest: Digest) -> None:
        """Commit a fully written temp file and record where it landed.

        The dedup short-circuit comes first and is the entire payoff of content
        addressing: if the digest is already committed, the bytes we just
        streamed are byte-for-byte identical to bytes we already hold, so the
        temp is dropped and nothing is rewritten. That is also why re-PUTting an
        object is nearly free.
        """
        if await self.contains(digest):
            await asyncio.to_thread(temp.unlink, True)
            metrics.DEDUP_HITS.inc()
            return

        size = await asyncio.to_thread(lambda: temp.stat().st_size)
        location = self._choose_location(size)
        backend = self.file_cas if location is BlobLocation.FILE_CAS else self.haystack
        await asyncio.to_thread(backend.commit_temp, temp, digest)

        self._locations[digest] = location
        metrics.BLOB_COUNT.inc()
        metrics.TOTAL_BYTES_STORED.inc(size)
        self._scrub_wakeup.set()

    async def commit_bytes(self, data: bytes) -> Digest:
        """Stage in-memory `data` under `tmp/` and commit it.

        For blobs that are genuinely small and already in RAM — a CDC chunk, a
        manifest. The streaming path never comes through here, by design.
        """
        digest = Digest.of(data)
        temp = TempEntry.unique_in(self.tmp_dir, "bytes")
        try:
            await asyncio.to_thread(temp.path.write_bytes, data)
            await self.commit_temp(temp.path, digest)
            temp.disarm()
        finally:
            temp.cleanup()
        return digest

    async def remove(self, digest: Digest) -> None:
        """Remove a committed blob if it exists, routing by the locator map."""
        location = self._locations.pop(digest, None)
        if location is None:
            return
        backend = self.file_cas if location is BlobLocation.FILE_CAS else self.haystack
        size = await asyncio.to_thread(backend.remove, digest)
        if size is not None:
            metrics.BLOB_COUNT.dec()
            metrics.TOTAL_BYTES_STORED.dec(size)

    # ── reads ───────────────────────────────────────────────────────────────

    def _require_not_quarantined(self, digest: Digest) -> None:
        if digest in self._quarantined:
            raise IntegrityError()

    async def open_blob(self, digest: Digest) -> BlobReader:
        """Open a committed blob, bounded to its length."""
        self._require_not_quarantined(digest)
        location = self.location(digest)
        if location is None:
            raise NoSuchKey()
        if location is BlobLocation.HAYSTACK:
            return await asyncio.to_thread(self.haystack.open_blob, digest)

        def _open() -> BoundedReader:
            handle = self.file_cas.open_blob(digest)
            length = self.file_cas.blob_path(digest).stat().st_size
            return BoundedReader(handle, length)

        return await asyncio.to_thread(_open)

    async def open_blob_range(self, digest: Digest, start: int, end: int) -> BlobReader:
        """Open the inclusive byte range `[start, end]` of a committed blob.

        Seeking rather than reading-and-discarding is what makes `Range` cheap:
        serving byte 4 GB of a 5 GB object costs one `lseek`, not 4 GB of I/O.
        (The cold tier cannot do this — see `lifecycle` — which is exactly the
        trade-off tiering buys.)
        """
        self._require_not_quarantined(digest)
        location = self.location(digest)
        if location is None:
            raise NoSuchKey()
        if location is BlobLocation.HAYSTACK:
            return await asyncio.to_thread(self.haystack.open_blob_range, digest, start, end)

        def _open() -> BoundedReader:
            path = self.file_cas.blob_path(digest)
            if not path.is_file():
                raise NoSuchKey()
            file_len = path.stat().st_size
            if start > end or end >= file_len:
                raise InvalidRequest(f"invalid range: start={start} end={end} file_len={file_len}")
            handle = path.open("rb")
            handle.seek(start)
            return BoundedReader(handle, end - start + 1)

        return await asyncio.to_thread(_open)

    # ── compaction ──────────────────────────────────────────────────────────

    async def compact_haystack(self) -> list[Digest]:
        """Run one Haystack compaction pass and patch the routing map.

        Live digests stay `HAYSTACK` — only their needle offsets moved. Digests
        the compaction dropped are removed from the map key by key. Never a full
        rebuild here: see `_rebuild_locations` on why that would clobber a
        concurrent commit.
        """
        dropped = await asyncio.to_thread(self.haystack.compaction)
        for digest in dropped:
            self._locations.pop(digest, None)
        return dropped

    async def run_compaction_loop(self, interval: float) -> None:
        """Compact on a timer until cancelled. A no-op tick when nothing is dirty."""
        logger.info("haystack volume compaction started", interval_secs=interval)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.compact_haystack()
            except AppError as err:
                logger.warning("haystack compaction pass failed", error=str(err))

    async def run_checkpoint_loop(self, interval: float = 0.5) -> None:
        """Fold the Haystack WAL into its snapshot whenever it is dirty.

        Separate from compaction and much more frequent: this only rewrites a
        small JSON file, and letting the log grow unbounded turns every boot into
        a long replay.
        """
        while True:
            await asyncio.sleep(interval)
            if not self.haystack.is_index_dirty:
                continue
            try:
                await asyncio.to_thread(self.haystack.checkpoint)
            except OSError as err:
                logger.warning("haystack index checkpoint failed", error=str(err))

    # ── continuous scrubbing ────────────────────────────────────────────────

    async def run_scrubber(self, rescan_interval: float) -> None:
        """Re-hash committed blobs forever, quarantining any that no longer match.

        Bit rot is silent: a flipped bit on disk produces a file that opens
        cleanly, has the right length, and hands the client corrupt bytes with a
        200. Content addressing is what makes it *detectable* — the name is the
        expected hash — and this loop is what makes it detected before a reader
        gets there rather than after.

        When the store is empty the loop parks on an event instead of spinning,
        and a commit wakes it immediately. When there are blobs it re-scans on a
        timer, because the point is to catch decay in bytes nobody has read in
        months.
        """
        logger.info("blob scrubber started", rescan_interval_secs=rescan_interval)
        while True:
            try:
                examined = await self.scrub_once()
            except (OSError, AppError) as err:
                logger.error("scrub pass failed", error=str(err))
                await asyncio.sleep(1.0)
                continue

            if examined == 0:
                metrics.SCRUB_IDLE_WAITS.inc()
                self._scrub_wakeup.clear()
                await self._scrub_wakeup.wait()
            else:
                self._scrub_wakeup.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._scrub_wakeup.wait(), timeout=rescan_interval)

    async def scrub_once(self) -> int:
        """One integrity pass over committed blobs. Returns how many were examined."""
        started = time.perf_counter()
        examined = await self._scrub_file_cas()
        examined += await asyncio.to_thread(self.haystack.scrub_once)
        # A Haystack quarantine lives in that backend's index; mirror it into the
        # store's read gate so `open_blob` refuses it without a lock round-trip.
        for digest in await asyncio.to_thread(self._haystack_quarantined):
            self._quarantined.add(digest)
            self._locations.pop(digest, None)
        metrics.SCRUB_PASSES.inc()
        metrics.SCRUB_PASS_DURATION.observe(time.perf_counter() - started)
        return examined

    def _haystack_quarantined(self) -> list[Digest]:
        live = set(self.haystack.indexed_digests())
        return [
            digest
            for digest, location in list(self._locations.items())
            if location is BlobLocation.HAYSTACK and digest not in live
        ]

    async def _scrub_file_cas(self) -> int:
        """Re-hash the FileCas tree, quarantining every content-address mismatch."""

        def _pass() -> tuple[int, int, list[tuple[Path, Digest]]]:
            examined = 0
            scanned = 0
            corrupt: list[tuple[Path, Digest]] = []
            for path in self.file_cas.iter_blob_files():
                digest = self.file_cas.digest_from_path(path)
                if digest is None or digest in self._quarantined:
                    continue
                examined += 1
                hasher = hashlib.sha256()
                with path.open("rb") as handle:
                    while chunk := handle.read(SCRUB_CHUNK):
                        scanned += len(chunk)
                        hasher.update(chunk)
                if hasher.hexdigest() != digest:
                    corrupt.append((path, digest))
            return examined, scanned, corrupt

        examined, scanned, corrupt = await asyncio.to_thread(_pass)
        metrics.SCRUB_BYTES_SCANNED.inc(scanned)
        metrics.SCRUB_BLOBS_VERIFIED.inc(examined - len(corrupt))
        for path, digest in corrupt:
            await self._quarantine_blob(path, digest)
        return examined

    async def _quarantine_blob(self, path: Path, digest: Digest) -> None:
        """Move a corrupt blob out of the tree and refuse it from now on.

        Moved rather than deleted: the bytes are evidence, and something has to
        explain *why* the object is gone when someone comes looking. The read
        gate is set before the move so no request can slip through the window
        between the two.
        """
        logger.warning(
            "scrub detected a content-address mismatch; quarantining",
            digest=str(digest),
        )
        self._quarantined.add(digest)
        self._locations.pop(digest, None)
        destination = self.quarantine_dir / str(digest)

        def _move() -> int | None:
            try:
                size = path.stat().st_size
                path.replace(destination)
                return size
            except OSError:
                return None

        size = await asyncio.to_thread(_move)
        if size is None:
            logger.error("failed to move a corrupt blob into quarantine", digest=str(digest))
        else:
            metrics.BLOB_COUNT.dec()
            metrics.TOTAL_BYTES_STORED.dec(size)
        metrics.SCRUB_CORRUPTIONS.inc()

    def record_location_for_test(self, digest: Digest, location: BlobLocation) -> None:
        """Plant a locator entry, to stage a race no public API can reach.

        `compact_haystack` must patch the specific digests a compaction dropped
        rather than rebuilding the whole map, because a rebuild snapshots the
        backends and would silently lose any commit that landed during the copy.
        Proving that needs an entry in the map that the backends do not know
        about yet — which is what a `commit_temp` mid-compaction looks like.
        """
        self._locations[digest] = location

    def is_quarantined(self, digest: Digest) -> bool:
        return digest in self._quarantined

    def close(self) -> None:
        """Release the Haystack append handle. Called from the lifespan."""
        self.haystack.close()
