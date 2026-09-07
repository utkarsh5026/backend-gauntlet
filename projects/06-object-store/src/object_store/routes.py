"""The HTTP surface: the path-style S3 API.

Path-style routing, streamed bodies, query-param verb dispatch, and the mapping
from exceptions to status codes live here. The vertical meat is in `streaming`,
`index`, `multipart` and `store` — a handler's job is to parse a request, hand
it to the right layer, and shape the answer.

## Query-param dispatch, and why the routes look strange

S3 overloads one URL with several operations, selected by query parameters:

```text
POST   /{bucket}/{key}?uploads                 InitiateMultipartUpload
PUT    /{bucket}/{key}?uploadId=…&partNumber=N UploadPart
POST   /{bucket}/{key}?uploadId=…              CompleteMultipartUpload
DELETE /{bucket}/{key}?uploadId=…              AbortMultipartUpload
PUT    /{bucket}?lifecycle                     PutBucketLifecycleConfiguration
```

That is not a design we would choose, but it is the design clients speak, so
`put_object` branches on `?uploadId` and `put_bucket` branches on `?lifecycle`.
Compatibility is the requirement; the tidier routing table would simply not
work with the AWS SDK.

## Keys are `{key:path}`

A key may contain `/` and is still one opaque string. Starlette's `:path`
converter is what stops it being split into segments, and it is the routing-level
half of the flat keyspace — `naming.encode_key` is the storage-level half.

## Bodies stream in both directions

The request body is an async iterator handed straight to V2's loop, never
`await request.body()`. The response body is a `StreamingResponse` over a
bounded reader. FastAPI has no default body-size limit to disable — the cap is
enforced in the stream loop where it belongs, counting bytes we actually
received rather than trusting `Content-Length`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import metrics
from .auth import PresignRequest, credentials_match, expires_in, sign
from .errors import AccessDenied, InvalidRequest, NoSuchKey
from .index import NewVersion, Precondition
from .lifecycle import LifecyclePolicy
from .manifest import open_range as open_manifest_range
from .naming import Bucket, Key
from .objects import LATEST, BlobKind, ObjectRef, ResolvedObject
from .s3_xml import (
    ListBucketParams,
    ListContent,
    complete_multipart_result,
    initiate_multipart_result,
    list_bucket_result,
    parse_complete_multipart_body,
)
from .state import AppState, get_state
from .streaming import ChecksumSpec, stream_cdc_to_store, stream_to_store

__all__ = ["router"]

logger = structlog.get_logger(__name__)

router = APIRouter()

DEFAULT_CONTENT_TYPE = "application/octet-stream"
"""S3's default when a client declares nothing."""

STREAM_CHUNK = 64 * 1024
"""Bytes per response-body read. Large enough to keep syscalls off the hot path,
small enough that a 5 GB GET never holds more than this in memory — which is the
download half of V2's O(1) promise."""


def xml_response(body: str, status_code: int = 200) -> Response:
    """An S3 XML response. SDK parsers reject a JSON body for these shapes."""
    return Response(content=body, status_code=status_code, media_type="application/xml")


def http_date(moment: datetime) -> str:
    """RFC 9110 IMF-fixdate — the only format `Last-Modified` accepts.

    The literal `GMT` is correct because every timestamp in this store is UTC;
    emitting a local offset here is a spec violation that most clients tolerate
    and some silently mis-parse.
    """
    return moment.strftime("%a, %d %b %Y %H:%M:%S GMT")


# ── health and presigning ───────────────────────────────────────────────────


@router.get("/healthz")
async def healthz() -> Response:
    """Liveness. Outside the object router, so it answers even when auth is on."""
    return Response(content="ok", media_type="text/plain")


class PresignBody(BaseModel):
    method: str
    bucket: str
    key: str
    expires_in_secs: int


class PresignResponse(BaseModel):
    url: str
    """Path and query only — no host. The caller knows its own endpoint, and
    baking one in here would break every deployment behind a proxy."""
    expires: int


@router.post("/presign")
async def presign(request: Request, body: PresignBody) -> PresignResponse:
    """Mint a presigned object URL.

    Requires the long-lived bearer credentials: minting a URL is *delegating*
    access, so it must be at least as protected as using it directly. A presign
    endpoint reachable by a presigned URL would let any holder of a read URL
    escalate to a write one.
    """
    state = get_state(request)
    if state.auth is None:
        raise AccessDenied()
    if not credentials_match(state.auth, request.headers.get("authorization")):
        raise AccessDenied()

    method = body.method.strip().upper()
    if method not in {"GET", "PUT", "DELETE", "HEAD", "POST"}:
        raise InvalidRequest(f"unsupported presign method: {body.method}")

    signed = sign(
        state.auth,
        PresignRequest(
            method=method,
            bucket=Bucket(body.bucket),
            key=Key(body.key),
            expires_at=expires_in(body.expires_in_secs),
        ),
    )
    return PresignResponse(url=signed.path_and_query, expires=signed.expires)


# ── buckets ─────────────────────────────────────────────────────────────────


@router.put("/{bucket}")
async def put_bucket(request: Request, bucket: str) -> Response:
    """`CreateBucket`, or `PutBucketLifecycleConfiguration` with `?lifecycle`."""
    state = get_state(request)
    name = Bucket(bucket)

    if "lifecycle" in request.query_params:
        return await _put_bucket_lifecycle(state, name, await request.body())

    await state.index.create_bucket(name)
    return Response(status_code=200)


async def _put_bucket_lifecycle(state: AppState, bucket: Bucket, body: bytes) -> Response:
    """Parse, validate and durably store a bucket's lifecycle policy.

    The policy is validated *before* it touches disk, so an incoherent rule —
    one that tiers a thing after it has already been deleted, or ages something
    at zero days — never gets persisted. A bad rule that reaches disk is a rule
    that starts deleting objects on the next sweep.

    Lifecycle config stays JSON rather than S3 XML: it is outside the
    wire-format SPEC item, and nothing in the SDK path reads it.
    """
    try:
        policy = LifecyclePolicy.model_validate_json(body)
    except ValueError as err:
        raise InvalidRequest(f"invalid lifecycle policy: {err}") from None
    policy.validate_coherent()

    await state.index.ensure_bucket(bucket)
    metadata = await state.index.load_bucket_metadata(bucket)
    metadata.lifecycle = policy
    await state.index.store_bucket_metadata(bucket, metadata)
    return Response(status_code=200)


@router.get("/{bucket}")
async def list_objects(request: Request, bucket: str) -> Response:
    """`ListObjectsV2`, or the lifecycle policy with `?lifecycle`."""
    state = get_state(request)
    name = Bucket(bucket)
    params = request.query_params

    if "lifecycle" in params:
        metadata = await state.index.load_bucket_metadata(name)
        return JSONResponse(content=metadata.lifecycle.model_dump(mode="json"))

    prefix = params.get("prefix", "")
    delimiter = params.get("delimiter") or None
    continuation = params.get("continuation-token") or None
    max_keys = _positive_int(params.get("max-keys"), default=1000, name="max-keys")

    listing = await state.index.list(name, prefix, delimiter, continuation, max_keys)

    contents = [
        ListContent(
            key=str(meta.key),
            last_modified=live.last_modified,
            etag=str(live.etag),
            size=live.size,
        )
        for meta in listing.objects
        if (live := meta.latest_live()) is not None
    ]
    return xml_response(
        list_bucket_result(
            ListBucketParams(
                name=str(name),
                prefix=prefix,
                delimiter=delimiter,
                max_keys=max_keys,
                is_truncated=listing.next_continuation_token is not None,
                next_continuation_token=listing.next_continuation_token,
                contents=contents,
                common_prefixes=listing.common_prefixes,
            )
        )
    )


# ── objects ─────────────────────────────────────────────────────────────────


@router.put("/{bucket}/{key:path}")
async def put_object(request: Request, bucket: str, key: str) -> Response:
    """Store an object, or stage a multipart part with `?uploadId&partNumber`.

    The body streams either way. Validation happens before a byte is read, so a
    bad bucket name costs nothing; and the body is stored through the
    content-addressed layout rather than any user-controlled path, so a
    traversal-shaped key cannot escape the data dir no matter what it contains.
    """
    state = get_state(request)
    name = Bucket(bucket)
    object_key = Key(key)
    params = request.query_params

    with metrics.InFlightUpload():
        body = request.stream()

        upload_id = params.get("uploadId")
        raw_part = params.get("partNumber")
        if upload_id and raw_part is not None:
            part_number = _positive_int(raw_part, default=0, name="partNumber")
            part = await state.multipart.upload_part(
                upload_id, part_number, body, state.max_object_size
            )
            return Response(status_code=200, headers={"etag": str(part.etag)})

        checksum = ChecksumSpec.from_headers(dict(request.headers))
        content_type = request.headers.get("content-type", DEFAULT_CONTENT_TYPE)
        precondition = _write_precondition(request)

        if state.settings.cdc.enabled:
            stored = await stream_cdc_to_store(
                state.store, body, state.max_object_size, checksum, state.settings.cdc
            )
        else:
            stored = await stream_to_store(state.store, body, state.max_object_size, checksum)

        # Blob-then-pointer: the bytes are durable in the CAS before this line,
        # so a crash here leaves a reclaimable orphan rather than a dangling key.
        await state.index.put(
            name,
            object_key,
            NewVersion(
                digest=stored.digest,
                etag=stored.etag,
                size=stored.size,
                content_type=content_type,
                blob_kind=stored.blob_kind,
            ),
            precondition,
        )

    logger.info("object stored", bucket=name, key=object_key, size=stored.size)
    return Response(status_code=200, headers={"etag": str(stored.etag)})


def _write_precondition(request: Request) -> Precondition:
    """Turn a write's conditional headers into a guard `index.put` enforces.

    Only the two cases S3 gives meaning to on a PUT:

    - `If-None-Match: *` → create-once, and it wins if both are present.
    - `If-Match: <etag>` → compare-and-swap.

    A non-`*` `If-None-Match` on a PUT is not a compatibility case anyone sends,
    so it is ignored rather than rejected — failing a request over a header we
    merely do not act on would be worse than honouring the rest of it.
    """
    from .objects import ETag

    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None and if_none_match.strip() == "*":
        return Precondition.if_none_match_star()

    if_match = request.headers.get("if-match")
    if if_match is not None:
        return Precondition.if_match(ETag(if_match))

    return Precondition.none()


@router.get("/{bucket}/{key:path}")
async def get_object(request: Request, bucket: str, key: str) -> Response:
    """Stream an object back, honouring `Range` and `If-None-Match`.

    Looks up the pointer (V3), opens the blob (V1) and streams the file as the
    response body, so the bytes never all sit in memory at once.

    `Range: bytes=a-b` produces a `206` with `Content-Range` and reads only that
    slice — which is what makes this store usable for video seeking and
    resumable downloads. `If-None-Match` matching the ETag produces a `304` with
    no body, and it is checked *before* the blob is opened: the whole point is
    to avoid the transfer.
    """
    state = get_state(request)
    meta = await _resolve(state, bucket, key, request)

    if (not_modified := _check_if_none_match(request, meta)) is not None:
        return not_modified

    raw_range = request.headers.get("range")
    is_range = raw_range is not None
    start, end = _parse_range(raw_range) if raw_range is not None else (0, max(meta.size - 1, 0))
    if start > end:
        raise InvalidRequest(f"invalid range: start={start} end={end}")
    if meta.size == 0:
        start, end = 0, -1
    response_size = end - start + 1

    reader = await _open_reader(state, meta, start, end)

    headers = {
        "etag": str(meta.etag),
        "content-length": str(response_size),
        "last-modified": http_date(meta.last_modified),
        "accept-ranges": "bytes",
    }
    if is_range:
        headers["content-range"] = f"bytes {start}-{end}/{meta.size}"
        metrics.RANGE_REQUESTS_SERVED.inc()

    metrics.OBJECTS_GET.inc()
    return StreamingResponse(
        _iter_reader(reader),
        status_code=206 if is_range else 200,
        media_type=meta.content_type,
        headers=headers,
    )


@router.head("/{bucket}/{key:path}")
async def head_object(request: Request, bucket: str, key: str) -> Response:
    """The object's metadata headers with **no body** (`HeadObject`).

    Deliberately never opens the blob. HEAD exists precisely so a client can
    read `Content-Length`, `ETag`, `Content-Type` and `Last-Modified` without
    paying to transfer the bytes — opening the file here would quietly undo
    that, and nothing would fail visibly.
    """
    state = get_state(request)
    meta = await _resolve(state, bucket, key, request)

    if (not_modified := _check_if_none_match(request, meta)) is not None:
        return not_modified

    return Response(
        status_code=200,
        media_type=meta.content_type,
        headers={
            "etag": str(meta.etag),
            "content-length": str(meta.size),
            "last-modified": http_date(meta.last_modified),
            "accept-ranges": "bytes",
        },
    )


@router.delete("/{bucket}/{key:path}")
async def delete_object(request: Request, bucket: str, key: str) -> Response:
    """Delete an object, or abort a multipart upload with `?uploadId`.

    S3 delete is idempotent: removing a key that was never there still returns
    `204`. Clients retry deletes, and a 404 on the second attempt would make a
    successful retry look like a failure.
    """
    state = get_state(request)
    params = request.query_params

    if upload_id := params.get("uploadId"):
        await state.multipart.abort(upload_id)
        return Response(status_code=204)

    await state.index.delete(Bucket(bucket), Key(key), _object_ref(params.get("versionId")))
    return Response(status_code=204)


@router.post("/{bucket}/{key:path}")
async def post_object(request: Request, bucket: str, key: str) -> Response:
    """The two body-shaped multipart verbs, dispatched on query params."""
    state = get_state(request)
    name = Bucket(bucket)
    object_key = Key(key)
    params = request.query_params

    if "uploads" in params:
        content_type = request.headers.get("content-type", DEFAULT_CONTENT_TYPE)
        upload_id = await state.multipart.initiate(name, object_key, content_type)
        return xml_response(initiate_multipart_result(str(name), str(object_key), upload_id))

    if upload_id := params.get("uploadId"):
        parts = parse_complete_multipart_body(
            request.headers.get("content-type"), await request.body()
        )
        meta = await state.multipart.complete(upload_id, parts)
        live = meta.latest_live()
        if live is None:  # pragma: no cover - complete always writes a live version
            raise NoSuchKey()
        return xml_response(
            complete_multipart_result(str(meta.bucket), str(meta.key), str(live.etag))
        )

    raise InvalidRequest("unrecognised POST (expected ?uploads or ?uploadId)")


# ── helpers ─────────────────────────────────────────────────────────────────


async def _resolve(state: AppState, bucket: str, key: str, request: Request) -> ResolvedObject:
    """Resolve `(bucket, key, ?versionId)` to a live object."""
    return await state.index.resolve(
        Bucket(bucket), Key(key), _object_ref(request.query_params.get("versionId"))
    )


def _object_ref(raw_version_id: str | None) -> ObjectRef:
    if raw_version_id is None:
        return LATEST
    try:
        return ObjectRef.version(int(raw_version_id))
    except ValueError:
        raise InvalidRequest(f"invalid versionId {raw_version_id!r}") from None


def _check_if_none_match(request: Request, meta: ResolvedObject) -> Response | None:
    """`304 Not Modified` when the client already has these exact bytes.

    Compared against the unquoted ETag as well as the quoted form, because
    clients echo back whatever they were given and S3 quotes ETags on the wire.
    """
    header = request.headers.get("if-none-match")
    if header is None:
        return None
    candidate = header.strip()
    if candidate in {str(meta.etag), f'"{meta.etag}"', "*"}:
        return Response(status_code=304)
    return None


def _parse_range(raw: str) -> tuple[int, int]:
    """Parse `bytes=<start>-<end>` into inclusive offsets.

    Only the closed form S3's `GetObject` needs — both ends required. Open-ended
    suffixes (`bytes=N-`, `bytes=-N`) are rejected rather than guessed at,
    because guessing wrong on a range serves the wrong bytes with a `206` and
    the client has no way to notice.

    Bounds are not checked against the object here: the blob layer rejects an
    out-of-range slice once the actual size is known.
    """
    unit, separator, bounds = raw.partition("=")
    if not separator:
        raise InvalidRequest(f"Range header must be '<unit>=<start>-<end>', got {raw!r}")
    if unit.strip() != "bytes":
        raise InvalidRequest(f"Range unit must be 'bytes', got {unit!r}")

    raw_start, separator, raw_end = bounds.partition("-")
    if not separator:
        raise InvalidRequest(f"Range bounds must be '<start>-<end>', got {bounds!r}")
    try:
        return int(raw_start), int(raw_end)
    except ValueError:
        raise InvalidRequest(
            f"Range bounds must be non-negative integers, got {bounds!r}"
        ) from None


def _positive_int(raw: str | None, *, default: int, name: str) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise InvalidRequest(f"{name} must be an integer, got {raw!r}") from None
    if value < 0:
        raise InvalidRequest(f"{name} must not be negative, got {value}")
    return value


async def _open_reader(state: AppState, meta: ResolvedObject, start: int, end: int) -> Any:
    """Open the right reader for this object's storage strategy and tier.

    Three cases converge on one bounded reader, which is what lets the streaming
    response above stay ignorant of all of them: a whole hot blob (seek), a
    whole cold blob (decode from the start, skip, take), and a CDC manifest
    (walk the chunks, each resolving its own tier).
    """
    if meta.size == 0:
        return _EmptyReader()

    if meta.blob_kind is BlobKind.MANIFEST:
        return await open_manifest_range(state.store, state.lifecycle, meta.digest, start, end)

    from .lifecycle import Encoding
    from .store import BoundedReader

    physical = await state.lifecycle.locate(meta.digest)
    if physical.encoding is Encoding.RAW:
        return await state.store.open_blob_range(meta.digest, start, end)

    if end >= meta.size:
        raise InvalidRequest(f"invalid range: start={start} end={end} size={meta.size}")

    def _open_cold() -> BoundedReader:
        from .lifecycle import skip_bytes

        handle = state.lifecycle.codec.open_decompressed(physical.path)
        # A cold stream cannot seek — decoding from byte zero and discarding the
        # prefix is the whole cost of the cold tier, and stating it here rather
        # than hiding it is the point.
        skip_bytes(handle, start)
        return BoundedReader(handle, end - start + 1)

    return await asyncio.to_thread(_open_cold)


class _EmptyReader:
    """A zero-length blob's reader. Zero-byte objects are legal and common."""

    def read(self, _size: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        return None


async def _iter_reader(reader: Any) -> AsyncIterator[bytes]:
    """Drain a bounded reader into the response, a chunk at a time.

    Every read hops to a thread, so a slow disk stalls this coroutine rather
    than the event loop — the download-side mirror of V2's upload backpressure.
    The `finally` closes the handle even when the client disconnects mid-stream,
    which is the case that leaks file descriptors if you forget it.
    """
    import time

    started = time.perf_counter()
    total = 0
    try:
        while chunk := await asyncio.to_thread(reader.read, STREAM_CHUNK):
            total += len(chunk)
            yield chunk
    finally:
        await asyncio.to_thread(reader.close)
        elapsed = time.perf_counter() - started
        if elapsed > 0 and total:
            metrics.DOWNLOAD_THROUGHPUT.observe(total / elapsed)
