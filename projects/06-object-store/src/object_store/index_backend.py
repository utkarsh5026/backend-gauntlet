"""Index-as-a-service — From the field, ungraded.

At S3 scale the key→location map is a **separately scaled metadata subsystem**,
not a struct inside the front-end process. Metadata operations and data
operations have completely different shapes: a PUT of a 5 GB object is one index
write and 5 GB of disk I/O, while a listing is thousands of index reads and no
data at all. Scaling them together means over-provisioning one to serve the
other.

Splitting them also makes the blob-then-pointer invariant *visible*: with the
index on the far side of a network call, "the blob is durable before the pointer
exists" stops being an ordering you can accidentally reverse in one function and
becomes an ordering between two systems. Kill the index process mid-PUT and you
get exactly the failure the invariant predicts — an orphan blob, never a
dangling key.

- `LocalIndex` delegates to the in-process `Index` (the default).
- `RemoteIndex` speaks HTTP JSON to the `object-store-index` process when
  `INDEX_URL` is set. Blobs still live under the front-end's own data dir.

Both satisfy `IndexBackend`, so nothing upstream knows which one it has.

## Errors have to survive the wire

The interesting part of the remote client is not the happy path — it is that a
`NoSuchKey` raised inside the index process must arrive at the S3 handler as a
`NoSuchKey`, not as "HTTP 404". So the service sends `{"code", "message"}` and
this module maps the code back onto the exception class. Lose that and every
error in the split deployment degrades to a generic 500, and the conditional
writes (which depend on 412 specifically) stop working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx
import structlog

from .bucket import BucketMetadata
from .errors import (
    AccessDenied,
    AppError,
    BucketAlreadyExists,
    EntityTooLarge,
    InvalidRequest,
    NoSuchBucket,
    NoSuchKey,
    NoSuchUpload,
    PreconditionFailed,
)
from .index import Index, Listing, NewVersion, Precondition
from .naming import Bucket, Key
from .objects import LATEST, BlobKind, Digest, ETag, ObjectMeta, ObjectRef, ResolvedObject

if TYPE_CHECKING:
    from .config import Settings
    from .store import Store

__all__ = ["IndexBackend", "LocalIndex", "RemoteIndex", "make_index_backend"]

logger = structlog.get_logger(__name__)

_ERROR_CODES: dict[str, type[AppError]] = {
    "NoSuchBucket": NoSuchBucket,
    "NoSuchKey": NoSuchKey,
    "NoSuchUpload": NoSuchUpload,
    "BucketAlreadyExists": BucketAlreadyExists,
    "InvalidRequest": InvalidRequest,
    "EntityTooLarge": EntityTooLarge,
    "PreconditionFailed": PreconditionFailed,
    "AccessDenied": AccessDenied,
}


@runtime_checkable
class IndexBackend(Protocol):
    """What the S3 layer needs from an index, wherever it lives."""

    async def create_bucket(self, bucket: Bucket) -> None: ...
    async def buckets(self) -> list[str]: ...
    async def ensure_bucket(self, bucket: Bucket) -> None: ...
    async def put(
        self,
        bucket: Bucket,
        key: Key,
        version: NewVersion,
        precondition: Precondition | None = None,
    ) -> ObjectMeta: ...
    async def get(self, bucket: Bucket, key: Key) -> ObjectMeta | None: ...
    async def resolve(
        self, bucket: Bucket, key: Key, object_ref: ObjectRef = LATEST
    ) -> ResolvedObject: ...
    async def delete(self, bucket: Bucket, key: Key, object_ref: ObjectRef = LATEST) -> None: ...
    async def list(
        self,
        bucket: Bucket,
        prefix: str = "",
        delimiter: str | None = None,
        continuation: str | None = None,
        max_keys: int = 1000,
    ) -> Listing: ...
    async def index_entries(self, bucket: Bucket) -> list[ObjectMeta]: ...
    async def gc(self) -> int: ...
    async def load_bucket_metadata(self, bucket: Bucket) -> BucketMetadata: ...
    async def store_bucket_metadata(self, bucket: Bucket, metadata: BucketMetadata) -> None: ...
    async def aclose(self) -> None: ...


class LocalIndex:
    """The in-process index, wrapped to satisfy `IndexBackend`.

    Almost all delegation, with one real difference: `ensure_bucket` returns a
    `Path` on `Index` because callers there want the directory, and returns
    nothing here because a path is meaningless across a network. Narrowing it is
    what lets the remote backend implement the same protocol at all.
    """

    __slots__ = ("index",)

    def __init__(self, index: Index) -> None:
        self.index = index

    async def create_bucket(self, bucket: Bucket) -> None:
        await self.index.create_bucket(bucket)

    async def buckets(self) -> list[str]:
        return await self.index.buckets()

    async def ensure_bucket(self, bucket: Bucket) -> None:
        await self.index.ensure_bucket(bucket)

    async def put(
        self,
        bucket: Bucket,
        key: Key,
        version: NewVersion,
        precondition: Precondition | None = None,
    ) -> ObjectMeta:
        return await self.index.put(bucket, key, version, precondition)

    async def get(self, bucket: Bucket, key: Key) -> ObjectMeta | None:
        return await self.index.get(bucket, key)

    async def resolve(
        self, bucket: Bucket, key: Key, object_ref: ObjectRef = LATEST
    ) -> ResolvedObject:
        return await self.index.resolve(bucket, key, object_ref)

    async def delete(self, bucket: Bucket, key: Key, object_ref: ObjectRef = LATEST) -> None:
        await self.index.delete(bucket, key, object_ref)

    async def list(
        self,
        bucket: Bucket,
        prefix: str = "",
        delimiter: str | None = None,
        continuation: str | None = None,
        max_keys: int = 1000,
    ) -> Listing:
        return await self.index.list(bucket, prefix, delimiter, continuation, max_keys)

    async def index_entries(self, bucket: Bucket) -> list[ObjectMeta]:
        return await self.index.index_entries(bucket)

    async def gc(self) -> int:
        return await self.index.gc()

    async def load_bucket_metadata(self, bucket: Bucket) -> BucketMetadata:
        return await self.index.load_bucket_metadata(bucket)

    async def store_bucket_metadata(self, bucket: Bucket, metadata: BucketMetadata) -> None:
        await self.index.store_bucket_metadata(bucket, metadata)

    async def aclose(self) -> None:
        """Nothing to close — there is no connection."""


def _precondition_wire(precondition: Precondition | None) -> dict[str, Any]:
    """Serialise a precondition for the wire.

    A separate shape from `Precondition` itself so the in-process type never has
    to grow serialisation concerns it does not otherwise need.
    """
    guard = precondition or Precondition.none()
    if guard.kind == "if_match":
        return {"type": "if_match", "etag": str(guard.etag)}
    if guard.kind == "if_none_match_star":
        return {"type": "if_none_match_star"}
    return {"type": "none"}


def precondition_from_wire(payload: dict[str, Any]) -> Precondition:
    kind = payload.get("type", "none")
    if kind == "if_match":
        return Precondition.if_match(ETag(payload["etag"]))
    if kind == "if_none_match_star":
        return Precondition.if_none_match_star()
    return Precondition.none()


def _object_ref_wire(object_ref: ObjectRef) -> dict[str, Any]:
    if object_ref.is_latest:
        return {"type": "latest"}
    return {"type": "version", "id": object_ref.version_id}


def object_ref_from_wire(payload: dict[str, Any]) -> ObjectRef:
    if payload.get("type") == "version":
        return ObjectRef.version(int(payload["id"]))
    return LATEST


class RemoteIndex:
    """HTTP client for the `object-store-index` process."""

    __slots__ = ("_client", "base_url")

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        # One client, reused: it holds the connection pool, and building a fresh
        # one per call would pay a TCP and TLS handshake on every index read —
        # which on the GET path is once per request.
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, *segments: str) -> str:
        from urllib.parse import quote

        path = "/".join(quote(segment, safe="") for segment in segments)
        return f"{self.base_url}/{path}"

    def _key_url(self, bucket: Bucket, key: Key, *tail: str) -> str:
        # `safe="/"` keeps the key's slashes as path separators, matching the
        # service's `{key:path}` route. Fully escaping them does *not* work:
        # ASGI servers normalise `%2F` back to a separator before routing, so
        # the encoded form never reaches a single-segment route at all. Every
        # other character is still escaped, so a key containing a literal `%2f`
        # is sent as `%252f` and stays distinct from one containing `/`.
        from urllib.parse import quote

        prefix = self._url("v1", "buckets", bucket, *tail)
        return f"{prefix}/{quote(key, safe='/')}"

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as err:
            raise AppError(f"index service unreachable: {err}") from err

        if response.is_success:
            if not response.content:
                return None
            return response.json()

        raise _error_from_response(response)

    async def create_bucket(self, bucket: Bucket) -> None:
        await self._request("PUT", self._url("v1", "buckets", bucket))

    async def buckets(self) -> list[str]:
        return await self._request("GET", self._url("v1", "buckets"))

    async def ensure_bucket(self, bucket: Bucket) -> None:
        # GET, not HEAD — see the service's route for why the body matters.
        await self._request("GET", self._url("v1", "buckets", bucket))

    async def put(
        self,
        bucket: Bucket,
        key: Key,
        version: NewVersion,
        precondition: Precondition | None = None,
    ) -> ObjectMeta:
        payload = await self._request(
            "PUT",
            self._key_url(bucket, key, "keys"),
            json={
                "digest": str(version.digest),
                "etag": str(version.etag),
                "size": version.size,
                "content_type": version.content_type,
                "blob_kind": version.blob_kind.value,
                "precondition": _precondition_wire(precondition),
            },
        )
        return ObjectMeta.model_validate(payload)

    async def get(self, bucket: Bucket, key: Key) -> ObjectMeta | None:
        payload = await self._request("GET", self._key_url(bucket, key, "keys"))
        return ObjectMeta.model_validate(payload) if payload else None

    async def resolve(
        self, bucket: Bucket, key: Key, object_ref: ObjectRef = LATEST
    ) -> ResolvedObject:
        payload = await self._request(
            "POST",
            self._key_url(bucket, key, "resolve"),
            json={"object_ref": _object_ref_wire(object_ref)},
        )
        return ResolvedObject(
            bucket=Bucket.from_trusted(payload["bucket"]),
            key=Key.from_trusted(payload["key"]),
            version_id=payload["version_id"],
            digest=Digest(payload["digest"]),
            etag=ETag(payload["etag"]),
            size=payload["size"],
            content_type=payload["content_type"],
            last_modified=_parse_datetime(payload["last_modified"]),
            blob_kind=BlobKind(payload.get("blob_kind", "whole")),
        )

    async def delete(self, bucket: Bucket, key: Key, object_ref: ObjectRef = LATEST) -> None:
        await self._request(
            "DELETE",
            self._key_url(bucket, key, "keys"),
            json={"object_ref": _object_ref_wire(object_ref)},
        )

    async def list(
        self,
        bucket: Bucket,
        prefix: str = "",
        delimiter: str | None = None,
        continuation: str | None = None,
        max_keys: int = 1000,
    ) -> Listing:
        params: dict[str, str] = {"prefix": prefix, "max_keys": str(max_keys)}
        if delimiter:
            params["delimiter"] = delimiter
        if continuation:
            params["continuation"] = continuation
        payload = await self._request(
            "GET", self._url("v1", "buckets", bucket, "list"), params=params
        )
        return Listing(
            objects=[ObjectMeta.model_validate(row) for row in payload["objects"]],
            common_prefixes=payload["common_prefixes"],
            next_continuation_token=payload.get("next_continuation_token"),
        )

    async def index_entries(self, bucket: Bucket) -> list[ObjectMeta]:
        payload = await self._request("GET", self._url("v1", "buckets", bucket, "entries"))
        return [ObjectMeta.model_validate(row) for row in payload]

    async def gc(self) -> int:
        payload = await self._request("POST", self._url("v1", "gc"))
        return int(payload["reclaimed"])

    async def load_bucket_metadata(self, bucket: Bucket) -> BucketMetadata:
        payload = await self._request("GET", self._url("v1", "buckets", bucket, "metadata"))
        return BucketMetadata.model_validate(payload)

    async def store_bucket_metadata(self, bucket: Bucket, metadata: BucketMetadata) -> None:
        await self._request(
            "PUT",
            self._url("v1", "buckets", bucket, "metadata"),
            content=metadata.model_dump_json().encode("utf-8"),
            headers={"content-type": "application/json"},
        )


def _error_from_response(response: httpx.Response) -> AppError:
    """Rebuild the original exception from the service's JSON error body."""
    try:
        payload = response.json()
        code = str(payload["code"])
        message = str(payload.get("message", ""))
    except (ValueError, KeyError, TypeError):
        fallback = _STATUS_FALLBACK.get(response.status_code)
        if fallback is not None:
            return fallback()
        return AppError(f"index service HTTP {response.status_code} (unparseable error body)")
    factory = _ERROR_CODES.get(code)
    if factory is None:
        return AppError(f"index service: {code}: {message}")
    return factory(message) if message else factory()


_STATUS_FALLBACK: dict[int, type[AppError]] = {
    404: NoSuchKey,
    409: BucketAlreadyExists,
    400: InvalidRequest,
    412: PreconditionFailed,
    413: EntityTooLarge,
    403: AccessDenied,
}
"""Used only when a response has no body to read the code out of — a proxy that
stripped one, or a error the service did not shape.

Deliberately a last resort: the status code is coarser than the S3 code (404
alone covers `NoSuchBucket`, `NoSuchKey` and `NoSuchUpload`), so every endpoint
here is designed to answer with a body rather than lean on this."""


def _parse_datetime(raw: str):  # noqa: ANN202
    from datetime import datetime

    return datetime.fromisoformat(raw)


def make_index_backend(settings: Settings, store: Store) -> IndexBackend:
    """Pick a backend from configuration.

    An empty `INDEX_URL` means in-process, which is the default and what tests
    use. Setting it is the only thing needed to move metadata to its own
    process — the S3 layer's code does not change.
    """
    if settings.index_url:
        logger.info("using the remote index service", index_url=settings.index_url)
        return RemoteIndex(settings.index_url)
    return LocalIndex(Index(settings.data_dir, store))
