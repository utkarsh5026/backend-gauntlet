"""A single application error family that maps itself to an HTTP response.

This is the Python shape of what `error.rs` did with an enum: a small hierarchy
carrying its status code as a class attribute. Raising beats returning a result
type here — a request walks catalog -> demux -> plan -> build, and any of the
four can fail several frames below the handler. `raise UnknownAsset()` needs no
plumbing in the frames between.

Getting the variants *distinct* is graded. "that title isn't in the library"
(404), "that title exists but not at that bitrate" (404 with a different reason),
"segment 900 of a 300-segment asset" (404), and "your Range header asks for byte
5,000,000 of a 2 MB segment" (416) are four different client mistakes, and a
player debugging playback needs to tell them apart.

## The one variant that carries data

`RangeNotSatisfiable` holds the resource length, because a 416 is required by
RFC 9110 to answer with `Content-Range: bytes */<length>` — it is how a client
that guessed wrong learns the real size and retries correctly. Emitting that
header is *envelope*, so it lives here, wired and working. Deciding that a range
is unsatisfiable in the first place is V4's judgement and lives in `delivery.py`.

## The 5xx rule

Log the detail, return something generic. `MalformedMedia` is a 5xx on purpose
even though it reads like bad input: the file that failed to parse is the
*server's own*, placed there by whoever runs this, so a client can do nothing
about it and must not be told which path blew up. That detail belongs in the log
line — never in the body, which would turn a parser bug into a filesystem
disclosure.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "InvalidRequest",
    "MalformedMedia",
    "RangeNotSatisfiable",
    "SegmentOutOfRange",
    "UnknownAsset",
    "UnknownRendition",
    "app_error_handler",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this server turns into a response."""

    status_code: int = 500
    message: str = "internal server error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message

    def headers(self) -> dict[str, str]:
        """Extra response headers this error requires. Empty for most."""
        return {}


class UnknownAsset(AppError):
    """No asset with that name in the library."""

    status_code = 404
    message = "unknown asset"


class UnknownRendition(AppError):
    """The asset exists, but not at that rung of the bitrate ladder."""

    status_code = 404
    message = "unknown rendition"


class SegmentOutOfRange(AppError):
    """A segment index past the end of the segmented rendition (V2/V4).

    Distinct from `UnknownRendition` because it means something different to a
    player: the rendition is real and its playlist was served, so an index past
    the end says the client is working from a manifest that no longer matches the
    media — a caching problem, not a naming one.
    """

    status_code = 404
    message = "segment out of range"


class InvalidRequest(AppError):
    """The request was malformed — a bad asset/rendition name, a bad index."""

    status_code = 400
    message = "invalid request"


class RangeNotSatisfiable(AppError):
    """A `Range` the resource cannot answer — start past EOF, or reversed (V4).

    Carries the resource length so the response can say `bytes */<length>`; see
    the module docstring on why that header is plumbing rather than part of V4.
    """

    status_code = 416
    message = "range not satisfiable"

    def __init__(self, total: int, message: str | None = None) -> None:
        super().__init__(message)
        self.total = total

    def headers(self) -> dict[str, str]:
        return {"content-range": f"bytes */{self.total}"}


class MalformedMedia(AppError):
    """A source file that isn't a container we can demux (V1).

    A 5xx, not a 4xx — see the module docstring.
    """

    status_code = 500
    message = "malformed source media"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to its response.

    Typed against `Exception` because that is the signature Starlette's handler
    registry expects; the narrowing happens here.
    """
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc

    if exc.status_code >= 500:
        logger.error("request failed", error=str(exc), kind=type(exc).__name__)

    client_message = AppError.message if exc.status_code >= 500 else exc.message
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": client_message},
        headers=exc.headers(),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the AppError -> HTTP mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
