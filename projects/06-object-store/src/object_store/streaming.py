"""V2 — streaming bodies, end to end: bounded memory and backpressure.

This is where "10 KB on a laptop" and "5 GB in production" stop being the same
program. The request body is pulled one chunk at a time, written straight to a
temp file and fed to both hashers, so an object of *any* size costs O(1) memory.
`await request.body()` is the single bug this whole vertical exists to prevent —
it is one line, it works perfectly in every test you are likely to write, and it
OOM-kills the box the first time someone uploads a movie.

## Backpressure is implicit, and you should understand why

The loop only asks for the next chunk *after* the previous one has been written.
`await asyncio.to_thread(handle.write, chunk)` does not return until the write
does, so a slow disk slows the **client** rather than growing a buffer. Nothing
here implements flow control; flow control is what you get for free by refusing
to buffer. Add a queue between the socket and the disk to "smooth things out"
and you have traded backpressure for an unbounded memory leak with extra steps.

## Two hashers, one pass

SHA-256 is the content address (V1's name on disk) and MD5 is the S3 ETag. They
are computed over the same chunk in the same pass, because reading a 5 GB body
twice to hash it twice is exactly the cost this vertical exists to avoid. A
client-supplied `Content-MD5` or `x-amz-checksum-*` also needs no third pass: it
is necessarily one of these two, already computed.

## The unhappy paths are the point

A client that disconnects mid-PUT, a body that trips the size cap, a checksum
that does not match — each must leave nothing behind. Every exit runs through a
`TempEntry` guard whose `finally` unlinks the staged file, and the guard is
disarmed only after `commit_temp` has durably published the blob. The happy path
was never the hard part.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum

from . import metrics
from .config import CdcSettings
from .durable import TempEntry
from .errors import EntityTooLarge, InvalidRequest
from .manifest import ChunkRef, Manifest, commit_manifest
from .objects import BlobKind, Digest, ETag
from .store import Store

__all__ = [
    "ChecksumAlgorithm",
    "ChecksumSpec",
    "Stored",
    "stream_cdc_to_store",
    "stream_to_store",
]

BodyStream = AsyncIterator[bytes]
"""What Starlette's `request.stream()` yields — chunks, never the whole body."""


class ChecksumAlgorithm(StrEnum):
    """A checksum algorithm a client can ask us to verify."""

    SHA256 = "SHA256"
    MD5 = "MD5"

    @classmethod
    def parse(cls, raw: str) -> ChecksumAlgorithm:
        """Match `x-amz-checksum-algorithm` case-insensitively."""
        try:
            return cls[raw.strip().upper()]
        except KeyError:
            raise InvalidRequest(f"unsupported checksum algorithm {raw!r}") from None


@dataclass(frozen=True, slots=True)
class ChecksumSpec:
    """The checksum a client asked us to verify on this PUT, if any.

    One decision spans three headers — `Content-MD5` wins outright, otherwise
    `x-amz-checksum-algorithm` names the algorithm and `x-amz-checksum-<ALGO>`
    carries the value — so it is parsed once, here, rather than three times in
    the handler.
    """

    algorithm: ChecksumAlgorithm
    expected: str
    """Base64 of the **raw** digest, which is what S3 puts on the wire — not the
    hex we store as a `Digest` or an `ETag`."""

    @classmethod
    def from_headers(cls, headers: dict[str, str] | None) -> ChecksumSpec | None:
        """Parse the three-header negotiation, or `None` if the client sent none.

        Header lookups are case-insensitive because HTTP header names are;
        Starlette's mapping already handles that, and this accepts a plain dict
        for testability by lowercasing the keys it asks for.
        """
        if not headers:
            return None
        get = headers.get

        if (content_md5 := get("content-md5")) is not None:
            return cls(ChecksumAlgorithm.MD5, content_md5)

        raw_algorithm = get("x-amz-checksum-algorithm")
        if raw_algorithm is None:
            return None
        algorithm = ChecksumAlgorithm.parse(raw_algorithm)

        header_name = f"x-amz-checksum-{algorithm.value.lower()}"
        value = get(header_name)
        if value is None:
            raise InvalidRequest(f"missing {header_name} header")
        return cls(algorithm, value)

    def verify(self, sha256: bytes, md5: bytes) -> None:
        """Check the client's claim against the digests already computed.

        Whichever algorithm was named, its digest is one of the two the stream
        loop produced — that is the point of running both hashers — so this
        costs nothing beyond a base64 decode and a compare.

        Raises `InvalidRequest`, which is S3's `BadDigest`: the bytes that
        arrived are not the bytes the client meant to send, and the correct
        answer is to reject them and store nothing.
        """
        computed = sha256 if self.algorithm is ChecksumAlgorithm.SHA256 else md5
        try:
            expected = base64.b64decode(self.expected, validate=True)
        except (ValueError, TypeError):
            raise InvalidRequest(f"invalid base64 {self.algorithm.value} checksum") from None
        if expected != computed:
            raise InvalidRequest(
                f"BadDigest: {self.algorithm.value} checksum does not match the streamed body"
            )


@dataclass(frozen=True, slots=True)
class Stored:
    """The outcome of streaming one body into the store.

    For CDC, `digest` is the **manifest's** CAS name and `blob_kind` says so,
    while `size` and `etag` still describe the *logical* whole object — a client
    must never be able to tell which storage strategy served it.
    """

    digest: Digest
    """SHA-256 of the streamed bytes — the blob's content address (V1)."""

    etag: ETag
    """The single-PUT S3 ETag, `md5(bytes).hexdigest()`. Deliberately *not* the
    same value as the digest; see `objects.ETag`."""

    size: int
    """Bytes actually counted off the wire — never a client's Content-Length."""

    blob_kind: BlobKind = BlobKind.WHOLE

    @classmethod
    def whole(cls, digest: Digest, etag: ETag, size: int) -> Stored:
        return cls(digest, etag, size, BlobKind.WHOLE)

    @classmethod
    def manifest(cls, digest: Digest, etag: ETag, size: int) -> Stored:
        return cls(digest, etag, size, BlobKind.MANIFEST)


def _record_throughput(size: int, started: float) -> None:
    elapsed = time.perf_counter() - started
    if elapsed > 0:
        metrics.UPLOAD_THROUGHPUT.observe(size / elapsed)


async def stream_to_store(
    store: Store,
    body: BodyStream,
    max_size: int,
    checksum: ChecksumSpec | None = None,
) -> Stored:
    """Stream a request body to disk chunk by chunk and commit it as one blob.

    `max_size` caps the **running total**, not any single chunk: the accumulated
    size is checked after every chunk, so a body that dribbles past the cap in
    small pieces is still rejected — and rejected *early*, before the rest of it
    has been written, which is the difference between a 413 and a full disk.

    Raises `EntityTooLarge` past the cap, `InvalidRequest` on a checksum
    mismatch, and propagates I/O errors. On every one of those paths the guard
    unlinks the staged temp, so a rejected or interrupted upload leaves nothing.
    """
    started = time.perf_counter()
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    total = 0

    with TempEntry.unique_in(store.tmp_dir, "stream") as temp:
        handle = await asyncio.to_thread(temp.path.open, "wb")
        try:
            async for chunk in body:
                total += len(chunk)
                if total > max_size:
                    raise EntityTooLarge()
                sha.update(chunk)
                md5.update(chunk)
                # Awaiting this write *is* the backpressure. See the module docs.
                await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)

        # Verify before publishing: a mismatch must return from inside the
        # guard's scope so the staged bytes are unlinked and nothing durable is
        # ever created for a body the client itself says is wrong.
        if checksum is not None:
            checksum.verify(sha.digest(), md5.digest())

        stored = Stored.whole(Digest.from_raw(sha.digest()), ETag(md5.hexdigest()), total)
        await store.commit_temp(temp.path, stored.digest)
        temp.disarm()

    _record_throughput(stored.size, started)
    return stored


async def stream_cdc_to_store(
    store: Store,
    body: BodyStream,
    max_size: int,
    checksum: ChecksumSpec | None,
    settings: CdcSettings,
) -> Stored:
    """CDC PUT path: cut into content-defined chunks → CAS each → commit a manifest.

    Storage grain becomes the chunk; the index pointer becomes the manifest's
    digest. The whole-object MD5 and any client checksum are still computed over
    the logical plaintext, because the client's view of the object must not
    change just because we changed how it is stored.

    Note what is *not* here: a temp file for the whole object. Chunks are
    committed as they are cut, so memory stays bounded by one chunk (capped at
    `max_chunk`) rather than by the object.
    """
    from .cdc import CdcChunker

    started = time.perf_counter()
    chunker = CdcChunker(settings)
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    total = 0
    chunk_refs: list[ChunkRef] = []

    async for data in body:
        total += len(data)
        if total > max_size:
            raise EntityTooLarge()
        sha.update(data)
        md5.update(data)
        for chunk in chunker.push(data):
            chunk_refs.append(ChunkRef(digest=await store.commit_bytes(chunk), size=len(chunk)))

    for chunk in chunker.finish():
        chunk_refs.append(ChunkRef(digest=await store.commit_bytes(chunk), size=len(chunk)))

    if checksum is not None:
        checksum.verify(sha.digest(), md5.digest())

    # Blob-then-pointer, one level down: every chunk is durable before the
    # manifest that names them, and the manifest is durable before the index row
    # that names it.
    manifest_digest = await commit_manifest(store, Manifest.new(chunk_refs))
    stored = Stored.manifest(manifest_digest, ETag(md5.hexdigest()), total)

    _record_throughput(stored.size, started)
    return stored
