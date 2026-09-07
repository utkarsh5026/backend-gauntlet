"""Shared object-store values: digests, ETags, and the versioned index row.

These are what the verticals hand each other. V2 streams a body and produces a
`Digest` + `ETag` + size; V3 records that as an `ObjectMeta` row pointing a
`(bucket, key)` at the blob; V4 assembles parts into the same shape.

## Latest vs versions

A key owns an append-only history (`VersionEntry`) plus a mutable `latest`
pointer into it. An API call says which version it means with `ObjectRef`:
`LATEST` on the hot path, `ObjectRef.version(id)` to pin a historical one. Each
entry is either a live object or a delete marker — a marker is *not* "a live
object with a flag", it has no digest to open, which is why they are separate
model classes rather than one class with an optional digest.

## Why pydantic models rather than dataclasses

These rows are the on-disk format. `ObjectMeta` is what gets serialised into
`index/<bucket>/objects/<encoded-key>.json` and parsed back on every GET, so
validation and (de)serialisation are the same problem — which is what pydantic
is for. `ResolvedObject` is the one exception: it never touches disk, it is the
flattened view a handler wants after resolving, so it is a plain dataclass.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field

from .errors import InvalidRequest, NoSuchKey
from .naming import Bucket, Key, ValidatedStr

__all__ = [
    "BlobKind",
    "Digest",
    "ETag",
    "ObjectMeta",
    "ObjectRef",
    "ResolvedObject",
    "VersionEntry",
    "VersionId",
    "utc_now",
]

VersionId = int
"""Opaque id of one version under a key. Monotonic per key; never reused."""


def utc_now() -> datetime:
    """Timezone-aware `now`. Naive datetimes on disk are a bug waiting to bite."""
    return datetime.now(UTC)


class BlobKind(StrEnum):
    """What the index's digest actually names on disk.

    Rows written before CDC existed have no such field, so `WHOLE` is the
    default everywhere and old JSON keeps loading.
    """

    WHOLE = "whole"
    """`digest` is the SHA-256 of the full plaintext object (V1)."""

    MANIFEST = "manifest"
    """`digest` is the SHA-256 of a `manifest.Manifest` blob; the logical bytes
    are the concatenation of the chunks it names."""


class Digest(ValidatedStr):
    """A content address: the hex SHA-256 of a blob's bytes.

    In a content-addressed store this *is* the blob's name on disk (V1), which
    is what makes dedup free — identical bytes produce the same digest and get
    stored once.

    With CDC a live version may hold a *manifest* digest here instead
    (`BlobKind.MANIFEST`): still a CAS name, just finer grain underneath.
    """

    __slots__ = ()

    BYTE_LEN = 32
    """Raw SHA-256 length, before hex encoding."""

    LEN = 64
    """Hex-encoded length — `2 * BYTE_LEN`."""

    @classmethod
    def validate(cls, value: str) -> None:
        if len(value) != cls.LEN or not all(c in "0123456789abcdefABCDEF" for c in value):
            raise InvalidRequest(
                f"digest must be exactly {cls.LEN} ASCII hex characters, "
                f"got len={len(value)} {value[:16]!r}"
            )

    def __new__(cls, value: str) -> Self:
        cls.validate(value)
        # Normalise case so two spellings of one address can never be two
        # entries in the locator map.
        return str.__new__(cls, value.lower())

    @classmethod
    def of(cls, data: bytes) -> Digest:
        """The content address of `data` — the one place SHA-256 is named."""
        return cls.from_trusted(hashlib.sha256(data).hexdigest())

    @classmethod
    def from_raw(cls, raw: bytes) -> Digest:
        """Hex-encode the 32 raw bytes coming out of a finished hasher."""
        if len(raw) != cls.BYTE_LEN:
            raise InvalidRequest(f"digest must be exactly {cls.BYTE_LEN} bytes, got len={len(raw)}")
        return cls.from_trusted(raw.hex())


class ETag(ValidatedStr):
    """The S3 `ETag`. **Not** the content digest — see `multipart` for why.

    - single PUT → `md5(bytes).hexdigest()` (V2)
    - multipart  → `md5(concat(raw part md5s)).hexdigest() + "-" + N` (V4)

    Unvalidated on purpose: a client sends one back in `If-Match` and we compare
    it, so any string is a legal *input*. Rejecting a malformed one at
    construction would turn a failed precondition (412, correct) into a 400.
    """

    __slots__ = ()


class ObjectRef:
    """Which version of a key a call addresses.

    A request/resolve discriminator, never persisted: the history lives in
    `ObjectMeta`, this only picks an entry out of it.
    """

    __slots__ = ("version_id",)

    def __init__(self, version_id: VersionId | None = None) -> None:
        self.version_id = version_id

    @classmethod
    def version(cls, version_id: VersionId) -> ObjectRef:
        return cls(version_id)

    @property
    def is_latest(self) -> bool:
        return self.version_id is None

    def __repr__(self) -> str:
        return "LATEST" if self.is_latest else f"ObjectRef({self.version_id})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ObjectRef):
            return NotImplemented
        return self.version_id == other.version_id

    def __hash__(self) -> int:
        return hash(self.version_id)


LATEST = ObjectRef()
"""The current version — what every request means unless it says `?versionId=`."""


class LiveVersion(BaseModel):
    """A version that has bytes behind it."""

    type: Literal["live"] = "live"
    digest: Digest
    etag: ETag
    size: int
    content_type: str
    blob_kind: BlobKind = BlobKind.WHOLE
    """Defaults to `WHOLE` so index rows written before CDC still load."""


class DeleteMarker(BaseModel):
    """A tombstone version: the key reads as absent, history stays intact."""

    type: Literal["delete_marker"] = "delete_marker"


VersionKind = Annotated[LiveVersion | DeleteMarker, Field(discriminator="type")]


class VersionEntry(BaseModel):
    """One immutable version under a key."""

    id: VersionId
    last_modified: datetime
    kind: VersionKind

    @classmethod
    def live(
        cls,
        version_id: VersionId,
        digest: Digest,
        etag: ETag,
        size: int,
        content_type: str,
        blob_kind: BlobKind = BlobKind.WHOLE,
    ) -> VersionEntry:
        return cls(
            id=version_id,
            last_modified=utc_now(),
            kind=LiveVersion(
                digest=digest,
                etag=etag,
                size=size,
                content_type=content_type,
                blob_kind=blob_kind,
            ),
        )

    def as_live(self) -> LiveVersion | None:
        """The live payload, or `None` for a delete marker."""
        return self.kind if isinstance(self.kind, LiveVersion) else None


@dataclass(slots=True)
class ResolvedObject:
    """Hot-path view of a **live** version — what GET/HEAD/list need after resolve.

    Flattened deliberately: a handler wants `meta.size`, not
    `meta.versions[i].kind.size`, and it should be impossible to hold one of
    these for a delete marker.
    """

    bucket: Bucket
    key: Key
    version_id: VersionId
    digest: Digest
    etag: ETag
    size: int
    content_type: str
    last_modified: datetime
    blob_kind: BlobKind


class ObjectMeta(BaseModel):
    """What we persist about one key — V3's index row.

    `latest` is the mutable pointer; `versions` is append-only. An overwrite
    appends a new live entry and flips the pointer, so the previous version
    stays retrievable by id.

    `next_id` is a per-key counter that only ever climbs, and it is deliberately
    **not** recomputed from `versions`: derive it and deleting the newest
    version rewinds the counter, so the next PUT reuses that id and a client
    holding the old `?versionId=` silently gets different bytes. Storing it is
    what makes "never reused" actually true.
    """

    bucket: Bucket
    key: Key
    latest: VersionId
    versions: list[VersionEntry]
    next_id: VersionId = 0

    @classmethod
    def new_live(
        cls,
        bucket: Bucket,
        key: Key,
        digest: Digest,
        etag: ETag,
        size: int,
        content_type: str,
        blob_kind: BlobKind = BlobKind.WHOLE,
    ) -> ObjectMeta:
        """A fresh key with a single live version (id `1`)."""
        entry = VersionEntry.live(1, digest, etag, size, content_type, blob_kind)
        return cls(bucket=bucket, key=key, latest=entry.id, versions=[entry], next_id=2)

    def resolve(self, object_ref: ObjectRef) -> VersionEntry | None:
        """The version `object_ref` names, or `None` if that id is gone."""
        wanted = self.latest if object_ref.is_latest else object_ref.version_id
        return next((v for v in self.versions if v.id == wanted), None)

    def resolve_live(self, object_ref: ObjectRef) -> ResolvedObject:
        """Resolve to a live object.

        A missing key, an unknown version id and a delete marker all raise
        `NoSuchKey`: to a client they are the same fact — there are no bytes at
        that address — and distinguishing them would leak that the key once
        existed.
        """
        entry = self.resolve(object_ref)
        live = entry.as_live() if entry is not None else None
        if entry is None or live is None:
            raise NoSuchKey()
        return ResolvedObject(
            bucket=self.bucket,
            key=self.key,
            version_id=entry.id,
            digest=live.digest,
            etag=live.etag,
            size=live.size,
            content_type=live.content_type,
            last_modified=entry.last_modified,
            blob_kind=live.blob_kind,
        )

    def latest_live(self) -> ResolvedObject | None:
        """The current live object, or `None` if `latest` is a delete marker."""
        try:
            return self.resolve_live(LATEST)
        except NoSuchKey:
            return None

    def append_live(
        self,
        digest: Digest,
        etag: ETag,
        size: int,
        content_type: str,
        blob_kind: BlobKind = BlobKind.WHOLE,
    ) -> VersionId:
        """Append a new live version and flip `latest` to it."""
        highest = max((v.id for v in self.versions), default=0)
        # `max` with the stored counter, not just `highest + 1`: a version
        # deleted from the middle of history must not free its id for reuse.
        version_id = max(self.next_id, highest + 1)
        self.next_id = version_id + 1
        self.versions.append(
            VersionEntry.live(version_id, digest, etag, size, content_type, blob_kind)
        )
        self.latest = version_id
        return version_id

    def remove_version(self, version_id: VersionId) -> bool:
        """Drop one version by id; retarget `latest` if it was the one removed.

        Returns whether anything was removed. When history empties, `latest`
        lands on `0` and the caller should drop the whole row.
        """
        before = len(self.versions)
        self.versions = [v for v in self.versions if v.id != version_id]
        if len(self.versions) == before:
            return False
        if self.latest == version_id:
            self.latest = max((v.id for v in self.versions), default=0)
        return True

    def digests(self) -> list[Digest]:
        """Every digest this key's history references — GC's mark input."""
        return [live.digest for v in self.versions if (live := v.as_live()) is not None]
