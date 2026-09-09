"""One application error family that turns itself into an S3 error response.

Rust modelled this as an enum with an `IntoResponse` impl. In Python the
equivalent is a small exception hierarchy: the status code and the S3 error code
are class attributes, so adding a variant is one class and the mapping cannot
drift out of sync with the type.

Raising beats returning here for the same reason it did in Rust — a PUT walks
route → stream → store → index and any of those layers can fail four frames
below the handler — except that Python needs no `?` to propagate it.

## The 5xx rule

Log the detail, return something generic. `AppError.message` is what the client
sees for a 5xx; the real cause goes to the log line. An I/O error's message
names a path inside the data dir, and handing that to a caller turns a disk
problem into a filesystem disclosure.

## The S3 vocabulary is the contract

`NoSuchKey`, `NoSuchBucket`, `NoSuchUpload`, `PreconditionFailed` are not
decorative: real SDKs branch on the `<Code>` element, so retry logic, "create if
missing" flows and conditional writes all depend on the exact string. The status
code alone is not enough — 404 covers three different S3 codes.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import Response

__all__ = [
    "AccessDenied",
    "AppError",
    "BucketAlreadyExists",
    "EntityTooLarge",
    "IntegrityError",
    "InvalidRequest",
    "NoSuchBucket",
    "NoSuchKey",
    "NoSuchUpload",
    "PreconditionFailed",
    "app_error_handler",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)

GENERIC_5XX_MESSAGE = "internal server error"
"""What a client is told about any 5xx. The real reason goes to the log."""


class AppError(Exception):
    """Base for every error this store turns into an S3 XML response."""

    status_code: int = 500
    code: str = "InternalError"
    message: str = GENERIC_5XX_MESSAGE

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message

    @property
    def is_server_error(self) -> bool:
        return self.status_code >= 500

    def client_message(self) -> str:
        """The message safe to put on the wire — scrubbed for 5xx."""
        return GENERIC_5XX_MESSAGE if self.is_server_error else self.message

    def headers(self) -> dict[str, str]:
        """Extra response headers this error requires. Empty for most."""
        return {}


class NoSuchBucket(AppError):
    """No bucket with that name."""

    status_code = 404
    code = "NoSuchBucket"
    message = "no such bucket"


class NoSuchKey(AppError):
    """No object with that key (or that version) in the bucket."""

    status_code = 404
    code = "NoSuchKey"
    message = "no such key"


class NoSuchUpload(AppError):
    """No in-progress multipart upload with that id (V4)."""

    status_code = 404
    code = "NoSuchUpload"
    message = "no such upload"


class BucketAlreadyExists(AppError):
    """Tried to create a bucket that is already there."""

    status_code = 409
    code = "BucketAlreadyExists"
    message = "bucket already exists"


class InvalidRequest(AppError):
    """A malformed request — bad name, bad range, bad multipart arguments."""

    status_code = 400
    code = "InvalidRequest"
    message = "invalid request"


class EntityTooLarge(AppError):
    """An object or part went past the configured `MAX_OBJECT_SIZE` cap (V2)."""

    status_code = 413
    code = "EntityTooLarge"
    message = "entity too large"


class PreconditionFailed(AppError):
    """A conditional write's guard did not hold (`If-Match` / `If-None-Match`).

    S3's answer to a failed compare-and-swap: the ETag moved under you, or the
    key you asked to create-once already exists.
    """

    status_code = 412
    code = "PreconditionFailed"
    message = "precondition failed"


class AccessDenied(AppError):
    """Auth failed: missing, expired, or forged credentials.

    Never put signature material or the reason for the rejection in this
    message — "expired" and "bad MAC" must look identical to a caller probing
    for one.
    """

    status_code = 403
    code = "AccessDenied"
    message = "access denied"


class IntegrityError(AppError):
    """A blob whose bytes no longer match its content address (quarantined).

    A 5xx, and deliberately so even though it reads like a data problem: the
    corrupt file is *ours*, the client did nothing wrong, and the one thing that
    must not happen is serving the bytes anyway. See `store.Scrubber`.
    """

    status_code = 500
    code = "InternalError"
    message = "blob failed integrity check"


def error_xml(code: str, message: str) -> str:
    """The S3 `<Error><Code/><Message/></Error>` envelope.

    Local rather than in `s3_xml` to keep the import graph acyclic: `s3_xml`
    needs to raise `InvalidRequest` when a body will not parse, so it depends on
    this module and cannot be depended on by it.
    """
    from .s3_xml import escape

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Error><Code>{escape(code)}</Code><Message>{escape(message)}</Message></Error>"
    )


async def app_error_handler(_request: Request, exc: Exception) -> Response:
    """Render an `AppError` as its S3 XML response.

    Typed against `Exception` because that is the signature Starlette's handler
    registry expects; the narrowing happens here.
    """
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc

    if exc.is_server_error:
        logger.error("request failed", error=str(exc), kind=type(exc).__name__)

    return Response(
        content=error_xml(exc.code, exc.client_message()),
        status_code=exc.status_code,
        media_type="application/xml",
        headers=exc.headers(),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the `AppError` → S3 XML mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
