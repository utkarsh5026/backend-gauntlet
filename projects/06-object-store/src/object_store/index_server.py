"""HTTP surface for the index microservice (`object-store-index`).

An internal JSON API, deliberately **not** S3 path-style: this is a private
protocol between two of our own processes, and shaping it like S3 would mean
inheriting XML, path-style bucket routing and query-param verb dispatch for no
benefit. Nothing outside the deployment should ever call it.

Errors are JSON `{"code", "message"}` so `RemoteIndex` can map the code back to
the exact exception the index raised — see that module on why the status code
alone is not enough (404 covers three different S3 errors).

Key routes use a `{key:path}` converter for the same reason the S3 surface does:
a key may contain `/`, and percent-encoding it into one segment does not
survive — ASGI servers normalise `%2F` back to a separator *before* routing, so
`a%2Fb` and `a/b` arrive identically. Accepting the slashes as segments and
treating the remainder as one opaque string is the only spelling that
round-trips.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from .bucket import BucketMetadata
from .errors import AppError
from .index import Index, NewVersion
from .index_backend import object_ref_from_wire, precondition_from_wire
from .naming import Bucket, Key
from .objects import BlobKind, Digest, ETag

__all__ = ["create_index_app", "get_index", "index_error_handler", "router"]

logger = structlog.get_logger(__name__)


def get_index(request: Request) -> Index:
    """Pull the index off the app.

    The same pattern `routes.get_state` uses, and for the same reasons: nothing
    resolves at import time, and a test can stand up two services over different
    data dirs in one process.
    """
    index = getattr(request.app.state, "index", None)
    if not isinstance(index, Index):  # pragma: no cover - startup invariant
        raise RuntimeError("index service state was not initialised")
    return index


CurrentIndex = Annotated[Index, Depends(get_index)]

router = APIRouter()


async def index_error_handler(_request: Request, exc: Exception) -> Response:
    """Render an `AppError` as the JSON envelope `RemoteIndex` expects."""
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc
    if exc.is_server_error:
        logger.error("index service request failed", error=str(exc))
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.client_message()},
    )


@router.get("/healthz")
async def healthz() -> str:
    return "ok"


@router.get("/v1/buckets")
async def list_buckets(index: CurrentIndex) -> list[str]:
    return await index.buckets()


@router.put("/v1/buckets/{bucket}")
async def create_bucket(index: CurrentIndex, bucket: str) -> Response:
    await index.create_bucket(Bucket(bucket))
    return Response(status_code=200)


@router.get("/v1/buckets/{bucket}")
async def ensure_bucket(index: CurrentIndex, bucket: str) -> dict[str, str]:
    """Existence check.

    A `GET` rather than the more obvious `HEAD`: a `HEAD` response is forbidden
    from carrying a body, so a missing bucket could only answer with a bare 404
    — and 404 covers `NoSuchBucket`, `NoSuchKey` and `NoSuchUpload` alike. This
    is our own internal protocol, so it costs nothing to pick the verb that can
    carry the code.
    """
    await index.ensure_bucket(Bucket(bucket))
    return {"name": bucket}


@router.get("/v1/buckets/{bucket}/metadata")
async def get_bucket_metadata(index: CurrentIndex, bucket: str) -> BucketMetadata:
    return await index.load_bucket_metadata(Bucket(bucket))


@router.put("/v1/buckets/{bucket}/metadata")
async def put_bucket_metadata(
    index: CurrentIndex, bucket: str, metadata: BucketMetadata
) -> Response:
    await index.store_bucket_metadata(Bucket(bucket), metadata)
    return Response(status_code=200)


@router.get("/v1/buckets/{bucket}/list")
async def list_objects(
    index: CurrentIndex,
    bucket: str,
    prefix: str = "",
    delimiter: str | None = None,
    continuation: str | None = None,
    max_keys: Annotated[int, Query(ge=0)] = 1000,
) -> dict[str, Any]:
    listing = await index.list(Bucket(bucket), prefix, delimiter, continuation, max_keys)
    return {
        "objects": [meta.model_dump(mode="json") for meta in listing.objects],
        "common_prefixes": listing.common_prefixes,
        "next_continuation_token": listing.next_continuation_token,
    }


@router.get("/v1/buckets/{bucket}/entries")
async def index_entries(index: CurrentIndex, bucket: str) -> list[dict[str, Any]]:
    entries = await index.index_entries(Bucket(bucket))
    return [meta.model_dump(mode="json") for meta in entries]


@router.put("/v1/buckets/{bucket}/keys/{key:path}")
async def put_key(
    index: CurrentIndex, bucket: str, key: str, body: dict[str, Any]
) -> dict[str, Any]:
    version = NewVersion(
        digest=Digest(str(body["digest"])),
        etag=ETag(str(body["etag"])),
        size=int(body["size"]),
        content_type=str(body["content_type"]),
        blob_kind=BlobKind(body.get("blob_kind", "whole")),
    )
    meta = await index.put(
        Bucket(bucket),
        Key(key),
        version,
        precondition_from_wire(body.get("precondition", {})),
    )
    return meta.model_dump(mode="json")


@router.get("/v1/buckets/{bucket}/keys/{key:path}")
async def get_key(index: CurrentIndex, bucket: str, key: str) -> dict[str, Any] | None:
    meta = await index.get(Bucket(bucket), Key(key))
    return meta.model_dump(mode="json") if meta is not None else None


@router.delete("/v1/buckets/{bucket}/keys/{key:path}")
async def delete_key(
    index: CurrentIndex, bucket: str, key: str, body: dict[str, Any] | None = None
) -> Response:
    payload = (body or {}).get("object_ref", {})
    await index.delete(Bucket(bucket), Key(key), object_ref_from_wire(payload))
    return Response(status_code=204)


@router.post("/v1/buckets/{bucket}/resolve/{key:path}")
async def resolve_key(
    index: CurrentIndex, bucket: str, key: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = (body or {}).get("object_ref", {})
    resolved = await index.resolve(Bucket(bucket), Key(key), object_ref_from_wire(payload))
    return {
        "bucket": str(resolved.bucket),
        "key": str(resolved.key),
        "version_id": resolved.version_id,
        "digest": str(resolved.digest),
        "etag": str(resolved.etag),
        "size": resolved.size,
        "content_type": resolved.content_type,
        "last_modified": resolved.last_modified.isoformat(),
        "blob_kind": resolved.blob_kind.value,
    }


@router.post("/v1/gc")
async def gc(index: CurrentIndex) -> dict[str, int]:
    return {"reclaimed": await index.gc()}


def create_index_app(index: Index) -> FastAPI:
    """Build the index service's ASGI app over one `Index`."""
    app = FastAPI(
        title="object-store-index",
        summary="The (bucket, key) → blob metadata service for project 06.",
    )
    app.state.index = index
    app.add_exception_handler(AppError, index_error_handler)
    app.include_router(router)
    return app
