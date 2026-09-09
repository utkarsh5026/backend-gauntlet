"""Chunk manifests — the recipe that turns CDC chunks back into one object.

With chunk-level dedup the index does **not** point at an object's bytes. It
points at a small content-addressed *manifest* blob:

```text
index:  (bucket, key) → manifest digest M    (BlobKind.MANIFEST)
store:  objects/<M>   = ordered [ChunkRef, …]
        objects/<d_i> = one chunk's plaintext bytes
```

The client still sees one object — one ETag, one size, ranges over *logical*
bytes. Assembly is entirely internal, which is the property that lets the
storage grain change without the API changing.

## Invariants

- Each `ChunkRef.digest` is the SHA-256 of that chunk's **plaintext**
  (hash-then-compress — the same identity rule the cold tier follows).
- `sum(chunk.size)` equals the live version's logical size. A manifest that does
  not add up is a corrupt object, not a smaller one.
- GC must mark the manifest **and** every chunk it names. Miss the expansion and
  shared chunks get reaped while another key still needs them — the failure mode
  is silent, and it corrupts objects that were never touched.
- Blob-then-pointer, twice over: all chunks durable before the manifest, the
  manifest durable before the index flip.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

from pydantic import BaseModel

from .errors import InvalidRequest
from .objects import Digest

if TYPE_CHECKING:
    # Types only: `lifecycle` imports `store`, and wiring the concrete classes
    # in at runtime would drag the whole tier machinery into a module that only
    # needs to describe a reader.
    from .lifecycle import Lifecycle
    from .store import Store


class BlobReader(Protocol):
    """What every reader in this store looks like: bounded bytes and a close.

    A `Protocol` rather than a union, because four different things satisfy it —
    a FileCas file window, a Haystack needle, a decompressing cold stream, and
    the concatenation below — and none of them share a base class.
    """

    def read(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


__all__ = [
    "BlobReader",
    "ChunkRef",
    "ChunkSlice",
    "Manifest",
    "ManifestRangeReader",
    "chunk_digests",
    "commit_manifest",
    "load_manifest",
    "map_range",
    "open_range",
]


class ChunkRef(BaseModel):
    """One slice of a CDC-assembled object."""

    digest: Digest
    """Plaintext SHA-256 of the chunk — its CAS name under `objects/`."""
    size: int
    """Plaintext length of this chunk."""

    @classmethod
    def of(cls, chunk: bytes) -> ChunkRef:
        return cls(digest=Digest.of(chunk), size=len(chunk))


class Manifest(BaseModel):
    """The ordered recipe for one logical object."""

    FORMAT_VERSION: ClassVar[int] = 1

    version: int = 1
    """On-disk schema version. Manifests are content-addressed and therefore
    immutable, so a format change means new manifests coexist with old ones
    forever — the version is how a reader tells them apart."""

    chunks: list[ChunkRef]

    @classmethod
    def new(cls, chunks: list[ChunkRef]) -> Manifest:
        return cls(version=cls.FORMAT_VERSION, chunks=chunks)

    @property
    def logical_size(self) -> int:
        """The object's size as the client sees it: the sum of its chunks."""
        return sum(chunk.size for chunk in self.chunks)

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> Manifest:
        return cls.model_validate_json(data)


async def commit_manifest(store: Store, manifest: Manifest) -> Digest:
    """Commit `manifest` as its own content-addressed blob; return its digest.

    Hash the **manifest bytes**, not the object payload. The chunks were already
    hashed individually and their plaintext must already be durable before this
    runs — this blob is only the list of names.

    A manifest being content-addressed has a pleasant consequence: two objects
    that chunk identically produce the same manifest and share even that.
    """
    return await store.commit_bytes(manifest.to_bytes())


async def load_manifest(store: Store, digest: Digest) -> Manifest:
    """Load and parse the manifest stored under `digest`."""
    reader = await store.open_blob(digest)

    def _read() -> bytes:
        with reader:
            return reader.read()

    return Manifest.from_bytes(await asyncio.to_thread(_read))


async def chunk_digests(store: Store, digest: Digest) -> list[Digest]:
    """The chunk digests a manifest names — GC's mark-phase expansion."""
    manifest = await load_manifest(store, digest)
    return [chunk.digest for chunk in manifest.chunks]


@dataclass(frozen=True, slots=True)
class ChunkSlice:
    """The part of one chunk that contributes to a logical byte range."""

    digest: Digest
    offset: int
    """Offset within the chunk's plaintext."""
    length: int
    """Bytes to read from that offset."""


def map_range(manifest: Manifest, start: int, end: int) -> list[ChunkSlice]:
    """Map an inclusive logical range `[start, end]` onto chunk slices.

    A ranged GET over a chunked object skips whole chunks before `start`, then
    reads a suffix of the first overlapping chunk, all of the middle ones, and a
    prefix of the last. Chunk spans are half-open internally (`[begin, finish)`)
    because inclusive arithmetic invites off-by-ones at exactly the boundaries
    this function exists to get right; only the caller's `end` is inclusive.
    """
    exclusive_end = end + 1
    offset = 0
    slices: list[ChunkSlice] = []

    for chunk in manifest.chunks:
        if offset >= exclusive_end:
            break
        begin, finish = offset, offset + chunk.size
        offset = finish
        if finish <= start:
            continue
        slice_start = max(start, begin)
        slice_end = min(exclusive_end, finish)
        if slice_end > slice_start:
            slices.append(
                ChunkSlice(
                    digest=chunk.digest,
                    offset=slice_start - begin,
                    length=slice_end - slice_start,
                )
            )
    return slices


class ManifestRangeReader:
    """Concatenates ordered chunk-slice readers into one logical byte stream.

    Opened eagerly, one reader per slice, so a chunk that has gone missing
    surfaces as an error *before* the response starts rather than as a truncated
    body halfway through — once the first byte is on the wire the status code is
    already committed and there is no way to tell the client something is wrong.
    """

    __slots__ = ("_parts",)

    def __init__(self, parts: list[BlobReader]) -> None:
        self._parts: deque[BlobReader] = deque(parts)

    def read(self, size: int = -1, /) -> bytes:
        while self._parts:
            part = self._parts[0]
            chunk = part.read(size)
            if chunk:
                return chunk
            part.close()
            self._parts.popleft()
        return b""

    def close(self) -> None:
        while self._parts:
            self._parts.popleft().close()

    def __enter__(self) -> ManifestRangeReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


async def open_range(
    store: Store,
    lifecycle: Lifecycle,
    manifest_digest: Digest,
    start: int,
    end: int,
) -> ManifestRangeReader:
    """Stream logical bytes `[start, end]` by reading chunk blobs in order.

    Each chunk resolves its own tier: a manifest's chunks are independent blobs,
    shared with other objects, so some may be hot and some already cold. Reading
    them through `lifecycle` rather than `store` directly is what keeps that
    invisible to the caller.
    """
    manifest = await load_manifest(store, manifest_digest)
    parts: list[BlobReader] = []
    try:
        for chunk_slice in map_range(manifest, start, end):
            if chunk_slice.length == 0:
                continue
            parts.append(await lifecycle.open_chunk_slice(store, chunk_slice))
    except Exception:
        for part in parts:
            part.close()
        raise
    if not parts and start > end:
        raise InvalidRequest(f"invalid range: start={start} end={end}")
    return ManifestRangeReader(parts)
