"""V4 — multipart upload, and the S3 ETag that proves wire compatibility.

This is the protocol that lets a 5 GB upload survive a flaky network: split it
into parts, upload them in parallel and out of order, retry the ones that fail,
and assemble at the end. Without it a transfer that dies at 90% starts again
from zero, which for a large object over a bad link means it may never finish at
all.

An upload is a **session** identified by an `upload_id`. Parts are staged on
disk until the client `Complete`s (assemble) or `Abort`s (discard). Nothing is
visible under the target key until completion — a half-uploaded object is not a
half-object, it is no object.

## The ETag is the compatibility test, and it is deliberately weird

For a single PUT, `ETag = md5(bytes).hexdigest()`. For a multipart object it is
**not** the MD5 of the assembled bytes. It is:

```text
md5(concat(raw md5 of each part, in part order)).hexdigest() + "-" + N
```

where `N` is the part count. Two things follow. The `-N` suffix is how a client
*knows* the object was multipart and must not try to verify it by re-hashing the
bytes — without it, every SDK's integrity check would fail on every large
object. And the value is computable without ever reading the object back, which
is why S3 can return it the instant assembly finishes.

Get the concatenation wrong — hex instead of raw bytes, or parts in arrival
order instead of part-number order — and the value is well-formed, plausible,
and rejected by the AWS SDK. That is the line between "an HTTP file server" and
"S3-compatible", and it is why this is the one formula in the project worth
testing against a real client.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import structlog
from pydantic import BaseModel

from . import metrics
from .durable import TempEntry
from .errors import EntityTooLarge, InvalidRequest, NoSuchKey, NoSuchUpload
from .index import NewVersion, Precondition
from .index_backend import IndexBackend
from .naming import Bucket, Key
from .objects import BlobKind, Digest, ETag, ObjectMeta
from .store import Store

__all__ = ["MAX_PART_NUMBER", "Multipart", "PartETag", "multipart_etag"]

logger = structlog.get_logger(__name__)

MAX_PART_NUMBER = 10_000
"""S3's ceiling. Not arbitrary: it is what bounds the part list a `Complete`
body can contain, and therefore how much work one request can ask for."""

SESSION_FILE = "session.json"
MD5_RAW_LEN = 16
COPY_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class PartETag:
    """One entry in a part list: its number and its MD5.

    The client echoes these back on `Complete`, and each is re-verified against
    what we actually staged before its bytes are used.
    """

    part_number: int
    etag: ETag


class UploadSession(BaseModel):
    """Per-upload metadata, persisted beside the session's staged parts.

    Written at `initiate` and re-read at `complete`, so the finished object
    lands under the original bucket, key and content type **even if the process
    restarted in between** — which for an upload that legitimately takes hours
    is not a corner case.
    """

    bucket: Bucket
    key: Key
    content_type: str


def multipart_etag(part_md5s: list[bytes]) -> ETag:
    """The S3 multipart ETag over parts already in part-number order.

    A pure function, separated from the assembly loop so it can be property-
    tested directly against the formula rather than through an HTTP round trip.

    Note `part_md5s` are **raw 16-byte digests**, not hex strings: the outer MD5
    is taken over 16N bytes, and hashing the 32N hex characters instead produces
    a completely different, entirely plausible-looking answer.
    """
    joined = b"".join(part_md5s)
    return ETag(f"{hashlib.md5(joined).hexdigest()}-{len(part_md5s)}")


class Multipart:
    """In-progress multipart uploads: their staging areas, assembly, and abort."""

    def __init__(self, root: Path, store: Store, index: IndexBackend) -> None:
        self.root = Path(root) / "uploads"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.index = index

    # ── layout ──────────────────────────────────────────────────────────────

    def staging_dir(self, upload_id: str) -> Path:
        return self.root / upload_id

    def session_path(self, upload_id: str) -> Path:
        return self.staging_dir(upload_id) / SESSION_FILE

    def part_path(self, upload_id: str, part_number: int) -> Path:
        # Zero-padded so the directory sorts in part order, which makes an
        # interrupted upload readable with `ls` instead of needing a tool.
        return self.staging_dir(upload_id) / f"{part_number:05d}.part"

    async def _require_session(self, upload_id: str) -> Path:
        path = self.staging_dir(upload_id)
        if not await asyncio.to_thread(path.is_dir):
            raise NoSuchUpload()
        return path

    # ── the four verbs ──────────────────────────────────────────────────────

    async def initiate(self, bucket: Bucket, key: Key, content_type: str) -> str:
        """`CreateMultipartUpload` — open a session and return its `upload_id`.

        The bucket is checked *now* rather than at completion: letting a client
        upload 5 GB of parts and only then discovering the bucket does not exist
        is a spectacular waste of everyone's bandwidth.

        The id is a v4 UUID — unguessable on purpose, because it is the only
        thing standing between a session and anyone who wants to add parts to
        it.
        """
        await self.index.ensure_bucket(bucket)

        upload_id = str(uuid.uuid4())
        session = UploadSession(bucket=bucket, key=key, content_type=content_type)

        def _create() -> None:
            self.staging_dir(upload_id).mkdir(parents=True)
            self.session_path(upload_id).write_bytes(session.model_dump_json().encode("utf-8"))

        await asyncio.to_thread(_create)

        metrics.MULTIPART_INITIATED.inc()
        metrics.MULTIPART_OPEN_SESSIONS.inc()
        logger.info("multipart upload initiated", upload_id=upload_id, bucket=bucket, key=key)
        return upload_id

    async def upload_part(
        self,
        upload_id: str,
        part_number: int,
        body: AsyncIterator[bytes],
        max_part_size: int,
    ) -> PartETag:
        """`UploadPart` — stream one part into staging and return its MD5 ETag.

        Reuses V2's discipline exactly: pull a chunk, count it, cap the running
        total, hash it, write it, ask for the next. A part is just a smaller
        object, and it gets the same O(1) memory guarantee.

        Re-uploading the same `part_number` overwrites the previous staging file
        — that *is* the retry story, and it is why parts may arrive in any
        order and any number of times.
        """
        if not 1 <= part_number <= MAX_PART_NUMBER:
            raise InvalidRequest(
                f"part_number must be between 1 and {MAX_PART_NUMBER}, got {part_number}"
            )
        await self._require_session(upload_id)

        md5 = hashlib.md5()
        total = 0
        started = time.perf_counter()

        with TempEntry(self.part_path(upload_id, part_number)) as part:
            handle = await asyncio.to_thread(part.path.open, "wb")
            try:
                async for chunk in body:
                    total += len(chunk)
                    if total > max_part_size:
                        raise EntityTooLarge()
                    md5.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
            finally:
                await asyncio.to_thread(handle.close)
            part.disarm()

        etag = ETag(md5.hexdigest())

        metrics.MULTIPART_PARTS_UPLOADED.inc()
        metrics.MULTIPART_PART_BYTES.observe(total)
        elapsed = time.perf_counter() - started
        if elapsed > 0:
            metrics.MULTIPART_PART_THROUGHPUT.observe(total / elapsed)
        return PartETag(part_number, etag)

    async def complete(self, upload_id: str, parts: list[PartETag]) -> ObjectMeta:
        """`CompleteMultipartUpload` — assemble, commit (V1), index (V3).

        The order of operations matters at every step:

        - Sort by part number **first**. The client's list is in whatever order
          its threads finished, and concatenating in that order produces a
          corrupt object with a perfectly valid-looking ETag.
        - Validate the list before touching any bytes: an empty list or a
          duplicate part number is a client error, and finding out halfway
          through a 5 GB assembly wastes the whole copy.
        - Re-verify each part's staged MD5 against the client's claim as it is
          read. This is not paranoia about our own disk — it catches the client
          that retried part 3, got a different body onto it, and is now
          completing with a stale ETag.
        - Commit the assembled blob before writing the index row, holding V3's
          blob-then-pointer invariant.
        - Delete the staging directory only after the object is indexed. Crash
          before that and the session survives, so the client can retry
          `Complete` rather than re-upload every part.
        """
        ordered = sorted(parts, key=lambda part: part.part_number)
        _validate_part_list(ordered)

        staging = await self._require_session(upload_id)
        session = await asyncio.to_thread(self._read_session, upload_id)

        sha = hashlib.sha256()
        part_md5s: list[bytes] = []
        total = 0

        with TempEntry.unique_in(self.store.tmp_dir, "multipart") as temp:

            def _assemble() -> tuple[int, list[bytes]]:
                running = 0
                digests: list[bytes] = []
                with temp.path.open("wb") as out:
                    for part in ordered:
                        source = self.part_path(upload_id, part.part_number)
                        if not source.is_file():
                            raise InvalidRequest(f"no staged part {part.part_number}")
                        part_md5 = hashlib.md5()
                        with source.open("rb") as handle:
                            while chunk := handle.read(COPY_CHUNK):
                                part_md5.update(chunk)
                                sha.update(chunk)
                                out.write(chunk)
                                running += len(chunk)
                        staged = part_md5.hexdigest()
                        if staged != part.etag:
                            raise InvalidRequest(
                                f"part {part.part_number} etag mismatch: "
                                f"client {part.etag} != staged {staged}"
                            )
                        digests.append(part_md5.digest())
                    out.flush()
                    os.fsync(out.fileno())
                return running, digests

            total, part_md5s = await asyncio.to_thread(_assemble)

            digest = Digest.from_raw(sha.digest())
            etag = multipart_etag(part_md5s)

            await self.store.commit_temp(temp.path, digest)
            temp.disarm()

        meta = await self.index.put(
            session.bucket,
            session.key,
            NewVersion(
                digest=digest,
                etag=etag,
                size=total,
                content_type=session.content_type,
                blob_kind=BlobKind.WHOLE,
            ),
            Precondition.none(),
        )

        await asyncio.to_thread(shutil.rmtree, staging, True)

        live = meta.latest_live()
        if live is None:  # pragma: no cover - put always writes a live version
            raise NoSuchKey()

        metrics.MULTIPART_COMPLETED.inc()
        metrics.MULTIPART_OPEN_SESSIONS.dec()
        metrics.MULTIPART_OBJECT_BYTES.observe(live.size)
        logger.info(
            "multipart upload completed",
            upload_id=upload_id,
            bucket=meta.bucket,
            key=meta.key,
            size=live.size,
            part_count=len(ordered),
        )
        return meta

    async def abort(self, upload_id: str) -> None:
        """`AbortMultipartUpload` — discard a session and reclaim its parts.

        The only way staged bytes are ever freed on the unhappy path, which is
        why `MULTIPART_OPEN_SESSIONS` climbing is a real alert: every session
        opened and never finished is disk nobody will reclaim.
        """
        staging = await self._require_session(upload_id)
        await asyncio.to_thread(shutil.rmtree, staging, True)

        metrics.MULTIPART_ABORTED.inc()
        metrics.MULTIPART_OPEN_SESSIONS.dec()

    def _read_session(self, upload_id: str) -> UploadSession:
        path = self.session_path(upload_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise NoSuchUpload() from None
        return UploadSession.model_validate_json(raw)

    async def sessions_older_than(self, cutoff: float) -> list[str]:
        """Upload ids whose staging directory predates `cutoff` (a unix time).

        The lifecycle sweep's input for `abort_multipart_after_days`. Staged
        parts are the one kind of storage in this system that nothing else ever
        reclaims — no key references them, so GC cannot see them — which is
        exactly why S3 has this rule.
        """

        def _scan() -> list[str]:
            stale: list[str] = []
            for entry in self.root.iterdir():
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    stale.append(entry.name)
            return stale

        return await asyncio.to_thread(_scan)


def _validate_part_list(ordered: list[PartETag]) -> None:
    """Reject an empty or duplicate-numbered part list before assembly begins."""
    if not ordered:
        raise InvalidRequest("complete requires at least one part")
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous.part_number == current.part_number:
            raise InvalidRequest("duplicate part numbers in complete request")
