"""V3 — the bucket/key namespace, a crash-safe index, prefix listing, and GC.

This maps `(bucket, key) → blob` and owns the rules that keep that mapping
consistent with V1's blobs across crashes and deletes. Three ideas to hold:

**The keyspace is flat.** `a/b/c.jpg` is one opaque key; the slashes mean
nothing to storage. `ListObjectsV2` only *pretends* it is a tree, by rolling
keys that share a prefix up to the next delimiter into a "common prefix". That
collapse is the entire illusion of folders in S3, and it is computed per
request — there is no directory anywhere to get out of sync.

**The write order is a crash-consistency contract.** V2 commits the blob to disk
*before* V3 records the pointer. Hold that invariant and a crash in between
leaves an unreferenced blob — garbage the GC reclaims, costing disk. Reverse it
and a crash leaves a key pointing at a blob that does not exist, which costs
*data*: the object is permanently unreadable and nothing on disk says why. One
direction is a cleanup problem, the other is corruption.

**Delete drops the pointer, not the bytes.** Dedup means another key may share
that digest, so `rm` on delete would silently destroy an unrelated object.
Reclamation is a separate mark-and-sweep, and it has to be careful not to reap a
blob belonging to a PUT that committed its bytes but has not written its index
entry *yet* — see `gc`.

## Storage shape

```text
index/<bucket>/metadata.json         bucket-level state (lifecycle policy)
index/<bucket>/objects/<enc-key>.json  one row per key: the version history
index/<bucket>/tmp/                    in-flight rows, staged for the atomic rename
```

One file per key rather than one big index file, because the atomic unit of
update is a key: `atomic_write` gives per-key durability with no write-ahead log
and no global lock, and two writers touching different keys never contend.
`<enc-key>` is `naming.encode_key`, so a key containing `/` or `..` still lands
as one flat filename.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

from . import metrics
from .bucket import BucketMetadata
from .durable import TempEntry, atomic_write
from .errors import (
    AppError,
    BucketAlreadyExists,
    NoSuchBucket,
    NoSuchKey,
    PreconditionFailed,
)
from .naming import Bucket, Key
from .objects import (
    LATEST,
    BlobKind,
    Digest,
    ETag,
    ObjectMeta,
    ObjectRef,
    ResolvedObject,
)
from .store import Store

__all__ = [
    "Index",
    "Listing",
    "NewVersion",
    "Precondition",
]

logger = structlog.get_logger(__name__)

OBJECTS_DIR = "objects"
TMP_DIR = "tmp"

GC_GRACE_SECONDS = 60.0
"""How long a blob must sit unreferenced before GC may reclaim it.

Belt to the tmp-scan's braces. The scan already protects a PUT whose index row
is staged; the grace window covers the smaller gap where bytes are committed but
the row has not been staged yet. Tests override it to zero — waiting a minute to
assert that reclamation happens is not a test, it is a nap."""


@dataclass(slots=True)
class NewVersion:
    """Everything `Index.put` records about newly stored bytes.

    Bundled so `put`'s signature stays about *addressing* — which key, under
    what precondition — rather than carrying five payload fields alongside.
    """

    digest: Digest
    etag: ETag
    size: int
    content_type: str
    blob_kind: BlobKind = BlobKind.WHOLE


class Precondition:
    """The guard a conditional write must satisfy.

    Evaluated *inside* the per-key lock, so the check and the pointer flip are
    one atomic step. Checking outside the lock would make `If-Match` a race with
    a wider window than the thing it exists to prevent.
    """

    __slots__ = ("etag", "kind")

    def __init__(self, kind: str = "none", etag: ETag | None = None) -> None:
        self.kind = kind
        self.etag = etag

    @classmethod
    def none(cls) -> Precondition:
        """Plain create-or-overwrite."""
        return cls("none")

    @classmethod
    def if_match(cls, etag: ETag) -> Precondition:
        """`If-Match: <etag>` — compare-and-swap. A mismatch, or an absent key,
        is a failed precondition."""
        return cls("if_match", etag)

    @classmethod
    def if_none_match_star(cls) -> Precondition:
        """`If-None-Match: *` — create-once. Two racing creators, one winner."""
        return cls("if_none_match_star")

    def check(self, current: ObjectMeta | None) -> None:
        """Raise `PreconditionFailed` if this guard does not hold."""
        if self.kind == "none":
            return
        live = current.latest_live() if current is not None else None
        if self.kind == "if_match":
            if live is None or live.etag != self.etag:
                raise PreconditionFailed()
        elif self.kind == "if_none_match_star":
            if live is not None:
                raise PreconditionFailed()


@dataclass(slots=True)
class Listing:
    """One page of a `ListObjectsV2` response."""

    objects: list[ObjectMeta]
    """Keys on this page that were not rolled up into a common prefix."""

    common_prefixes: list[str]
    """The faked subdirectories. With `delimiter=/` and `prefix=a/`, keys
    `a/b/c` and `a/b/d` both collapse to the single prefix `a/b/`."""

    next_continuation_token: str | None
    """Set when the page was truncated at `max_keys`; pass it back to resume."""


class Index:
    """The `(bucket, key) → blob` map, its listing, and its reclamation."""

    def __init__(self, root: Path, store: Store, *, gc_grace: float = GC_GRACE_SECONDS) -> None:
        self.root = Path(root) / "index"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.gc_grace = gc_grace
        # One lock per key, created on demand. `defaultdict` under the GIL is
        # enough to make creation safe: no `await` happens between the lookup
        # and the insert, so two coroutines cannot end up with different locks
        # for one key.
        self._key_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    # ── layout ──────────────────────────────────────────────────────────────

    def bucket_dir(self, bucket: Bucket) -> Path:
        return self.root / bucket

    def objects_dir(self, bucket: Bucket) -> Path:
        return self.bucket_dir(bucket) / OBJECTS_DIR

    def tmp_dir(self, bucket: Bucket) -> Path:
        return self.bucket_dir(bucket) / TMP_DIR

    def index_path(self, bucket: Bucket, key: Key) -> Path:
        return self.objects_dir(bucket) / f"{key.as_filename()}.json"

    # ── buckets ─────────────────────────────────────────────────────────────

    async def create_bucket(self, bucket: Bucket) -> None:
        """Create an empty bucket. Raises `BucketAlreadyExists` if it is there."""

        def _create() -> None:
            path = self.bucket_dir(bucket)
            if path.exists():
                raise BucketAlreadyExists()
            (path / OBJECTS_DIR).mkdir(parents=True)
            (path / TMP_DIR).mkdir(parents=True)
            BucketMetadata.new().store(path)

        await asyncio.to_thread(_create)

    async def buckets(self) -> list[str]:
        def _list() -> list[str]:
            return sorted(p.name for p in self.root.iterdir() if p.is_dir())

        return await asyncio.to_thread(_list)

    async def ensure_bucket(self, bucket: Bucket) -> Path:
        """The bucket's directory, or `NoSuchBucket`."""
        path = self.bucket_dir(bucket)
        if not await asyncio.to_thread(path.is_dir):
            raise NoSuchBucket()
        return path

    async def load_bucket_metadata(self, bucket: Bucket) -> BucketMetadata:
        path = await self.ensure_bucket(bucket)
        return await asyncio.to_thread(BucketMetadata.load, path)

    async def store_bucket_metadata(self, bucket: Bucket, metadata: BucketMetadata) -> None:
        path = await self.ensure_bucket(bucket)
        await asyncio.to_thread(metadata.store, path)

    # ── the key pointer ─────────────────────────────────────────────────────

    async def put(
        self,
        bucket: Bucket,
        key: Key,
        version: NewVersion,
        precondition: Precondition | None = None,
    ) -> ObjectMeta:
        """Publish a new live version for `(bucket, key)` if the guard holds.

        Holds the key's lock across read-current → check → write-pointer, so two
        concurrent writers cannot interleave. Without it the classic lost update
        applies — both read the same history, both append, the second write wins
        and the first version vanishes from the history it should have joined —
        and `If-Match`'s compare-and-swap would be a compare *then* a swap, with
        a gap in between.

        The lock is cheap because the blob is already durable in the CAS by the
        time this is called: it spans a small JSON read and an atomic rename,
        not the upload. That ordering is the whole reason a 5 GB PUT does not
        block other writers to the same key for five minutes.
        """
        guard = precondition or Precondition.none()

        async with self._key_locks[(bucket, key)]:
            current = await self.get(bucket, key)
            guard.check(current)

            if current is None:
                meta = ObjectMeta.new_live(
                    bucket,
                    key,
                    version.digest,
                    version.etag,
                    version.size,
                    version.content_type,
                    version.blob_kind,
                )
            else:
                meta = current
                meta.append_live(
                    version.digest,
                    version.etag,
                    version.size,
                    version.content_type,
                    version.blob_kind,
                )

            await self._write_meta(meta)

        metrics.OBJECTS_PUT.inc()
        metrics.OBJECT_SIZE_BYTES.observe(version.size)
        return meta

    async def _write_meta(self, meta: ObjectMeta) -> None:
        """Durably persist an index row.

        Staged under the bucket's own `tmp/` rather than a sibling of the
        destination, because GC scans that directory: a row sitting there is how
        an in-flight write tells the collector "these digests are spoken for,
        even though nothing committed references them yet".
        """
        destination = self.index_path(meta.bucket, meta.key)
        payload = meta.model_dump_json().encode("utf-8")

        def _write() -> None:
            tmp = self.tmp_dir(meta.bucket)
            tmp.mkdir(parents=True, exist_ok=True)
            with TempEntry.unique_in(tmp, meta.key.as_filename()) as temp:
                atomic_write(temp.path, destination, payload)
                temp.disarm()

        await asyncio.to_thread(_write)

    async def get(self, bucket: Bucket, key: Key) -> ObjectMeta | None:
        """The full row for `(bucket, key)`, or `None`.

        Reads only the JSON pointer — it never opens the underlying blob, which
        is what makes HEAD and conditional GET cheap regardless of object size.
        """
        return await asyncio.to_thread(_read_meta, self.index_path(bucket, key))

    async def resolve(
        self, bucket: Bucket, key: Key, object_ref: ObjectRef = LATEST
    ) -> ResolvedObject:
        """Resolve to a live object, or `NoSuchKey`."""
        meta = await self.get(bucket, key)
        if meta is None:
            raise NoSuchKey()
        return meta.resolve_live(object_ref)

    async def delete(self, bucket: Bucket, key: Key, object_ref: ObjectRef = LATEST) -> None:
        """Delete under `(bucket, key)`.

        `LATEST` drops the whole row and all its history — the non-versioned S3
        delete shape. A specific version removes just that entry, retargeting
        `latest` if it was the one removed, and drops the row when history
        empties.

        Either way the blobs stay: another key may share them. Reclamation is
        `gc`'s problem, which is why delete is O(1) and reclamation is lazy.
        """
        async with self._key_locks[(bucket, key)]:
            path = self.index_path(bucket, key)
            if object_ref.is_latest:
                # Idempotent: S3 returns 204 for deleting a key that was never
                # there, so a missing file is a success, not a 404.
                await asyncio.to_thread(path.unlink, True)
            else:
                meta = await self.get(bucket, key)
                if meta is None or object_ref.version_id is None:
                    raise NoSuchKey()
                if not meta.remove_version(object_ref.version_id):
                    raise NoSuchKey()
                if not meta.versions:
                    await asyncio.to_thread(path.unlink, True)
                else:
                    await self._write_meta(meta)

        metrics.OBJECTS_DELETED.inc()

    # ── listing ─────────────────────────────────────────────────────────────

    async def list(
        self,
        bucket: Bucket,
        prefix: str = "",
        delimiter: str | None = None,
        continuation: str | None = None,
        max_keys: int = 1000,
    ) -> Listing:
        """One page of a `ListObjectsV2`-style listing.

        The algorithm, in the order it has to happen:

        1. Read every row in the bucket and keep the live ones under `prefix`.
           A delete marker at `latest` is not listed — the key reads as absent.
        2. If a `delimiter` was given, split the *remainder* after `prefix` at
           its first occurrence. Keys with one become a common prefix (the
           folder); keys without stay as objects (the files). This is where the
           tree is invented, and it exists only for the duration of the request.
        3. Merge objects and prefixes into one list sorted by name, because S3
           orders them together — a client paging through must see a single
           ordered sequence, not files then folders.
        4. Drop everything at or before the continuation token, then cut at
           `max_keys` and hand back the last name kept as the next token.

        The token is the last name rather than an offset on purpose: an offset
        into a list that changes between pages skips or repeats keys, while
        "resume after this name" stays correct across concurrent writes.
        """
        # A missing bucket is `NoSuchBucket`, not an empty page. They are
        # different facts and clients branch on the difference: "the bucket is
        # empty" is a normal state to poll, "the bucket does not exist" means
        # create it or stop.
        await self.ensure_bucket(bucket)
        rows = await self._read_bucket_rows(bucket)

        objects = [
            meta for meta in rows if meta.key.startswith(prefix) and meta.latest_live() is not None
        ]

        common_prefixes: set[str] = set()
        if delimiter:
            leaves: list[ObjectMeta] = []
            for meta in objects:
                remainder = meta.key[len(prefix) :]
                position = remainder.find(delimiter)
                if position >= 0:
                    common_prefixes.add(prefix + remainder[: position + len(delimiter)])
                else:
                    leaves.append(meta)
            objects = leaves

        # `(name, is_prefix, payload)` — sorting on the name alone gives S3's
        # merged order, and the flag is what splits them back apart after the
        # page has been cut.
        items: list[tuple[str, bool, object]] = [(str(meta.key), False, meta) for meta in objects]
        items.extend((name, True, name) for name in common_prefixes)
        items.sort(key=lambda item: item[0])

        if continuation is not None:
            items = [item for item in items if item[0] > continuation]

        truncated = len(items) > max_keys
        next_token = items[max_keys - 1][0] if truncated and max_keys > 0 else None
        items = items[:max_keys]

        page_objects: list[ObjectMeta] = []
        page_prefixes: list[str] = []
        for _, is_prefix, payload in items:
            if is_prefix:
                page_prefixes.append(payload)  # type: ignore[arg-type]
            else:
                page_objects.append(payload)  # type: ignore[arg-type]

        return Listing(page_objects, page_prefixes, next_token)

    async def index_entries(self, bucket: Bucket) -> list[ObjectMeta]:
        """Every committed row in a bucket — what the lifecycle sweep walks."""
        return await self._read_bucket_rows(bucket)

    async def _read_bucket_rows(self, bucket: Bucket) -> list[ObjectMeta]:
        def _read() -> list[ObjectMeta]:
            return [
                meta
                for path in _index_files(self.objects_dir(bucket))
                if (meta := _read_meta(path)) is not None
            ]

        return await asyncio.to_thread(_read)

    # ── garbage collection ──────────────────────────────────────────────────

    async def gc(self) -> int:
        """Mark-and-sweep the blob store; return how many blobs were reclaimed.

        **Mark** gathers every digest referenced by a committed row *and* by an
        in-flight row staged under a bucket's `tmp/`. **Sweep** walks the blob
        tree and removes anything unreferenced that is also older than the grace
        window.

        The race this is built around: a PUT commits its blob, then writes its
        index row. A GC running in that gap sees a blob nothing references and,
        naively, deletes it — destroying an upload that is about to succeed. Two
        independent guards close it. The tmp scan catches writes whose row is
        staged; the grace window catches the smaller gap before staging. Either
        one alone leaves a hole, which is why both are here.

        A corrupt row under `tmp/` is skipped rather than fatal: a half-written
        temp is the *expected* shape of a crash mid-PUT, and letting one wedge
        the collector would mean a single bad crash stops all reclamation
        forever. A corrupt *committed* row is a different matter and propagates.
        """
        referenced = await self._collect_referenced_digests()
        cutoff = time.time() - self.gc_grace
        reclaimed = 0

        def _sweep_candidates() -> list[Digest]:
            candidates: list[Digest] = []
            for path in self.store.file_cas.iter_blob_files():
                digest = self.store.digest_from_path(path)
                if digest is None or digest in referenced:
                    continue
                if path.stat().st_mtime > cutoff:
                    continue
                candidates.append(digest)
            return candidates

        for digest in await asyncio.to_thread(_sweep_candidates):
            await self.store.remove(digest)
            metrics.GC_BLOBS_RECLAIMED.inc()
            reclaimed += 1

        return reclaimed

    async def _collect_referenced_digests(self) -> set[Digest]:
        """Every digest the store must keep: committed rows plus in-flight ones."""

        def _scan() -> tuple[list[ObjectMeta], list[ObjectMeta]]:
            committed: list[ObjectMeta] = []
            in_flight: list[ObjectMeta] = []
            for bucket_dir in self.root.iterdir():
                if not bucket_dir.is_dir():
                    continue
                for path in _index_files(bucket_dir / OBJECTS_DIR):
                    meta = _read_meta(path)
                    if meta is not None:
                        committed.append(meta)
                for path in _index_files(bucket_dir / TMP_DIR):
                    # Deliberately forgiving — see the docstring.
                    try:
                        meta = _read_meta(path)
                    except (ValueError, OSError):
                        continue
                    if meta is not None:
                        in_flight.append(meta)
            return committed, in_flight

        committed, in_flight = await asyncio.to_thread(_scan)

        referenced: set[Digest] = set()
        for meta in (*committed, *in_flight):
            await self._mark(meta, referenced)
        return referenced

    async def _mark(self, meta: ObjectMeta, referenced: set[Digest]) -> None:
        """Add one row's digests — and, for manifests, their chunks.

        The expansion is not optional. A manifest's own digest keeps the recipe
        alive but says nothing about the chunks, and those chunks are shared:
        reaping one because *this* object's manifest was the only thing pointing
        at it directly corrupts every other object that shares it.
        """
        from .manifest import chunk_digests

        for version in meta.versions:
            live = version.as_live()
            if live is None:
                continue
            referenced.add(live.digest)
            if live.blob_kind is BlobKind.MANIFEST:
                try:
                    referenced.update(await chunk_digests(self.store, live.digest))
                except AppError as err:
                    # An unreadable manifest means we cannot know what it
                    # protects, so reclaim nothing on its behalf rather than
                    # guess. Loud, because it is a real integrity problem.
                    logger.error(
                        "gc could not expand a manifest; skipping its chunks",
                        digest=str(live.digest),
                        error=str(err),
                    )


def _index_files(directory: Path) -> Iterable[Path]:
    """Regular files directly inside `directory`; empty if it does not exist."""
    try:
        return [path for path in directory.iterdir() if path.is_file()]
    except FileNotFoundError:
        return []


def _read_meta(path: Path) -> ObjectMeta | None:
    """Parse one index row, or `None` when the file is not there."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return ObjectMeta.model_validate(json.loads(raw))
