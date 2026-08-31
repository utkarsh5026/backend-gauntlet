"""HTTP surface: the producer + admin API.

Workers consume the queue out of band (see `worker`); this is how jobs get *in*
and how you inspect them.

Two routers, split by whether the route mutates: `public_router` serves liveness
and the read-only admin views, `protected_router` carries the enqueue and requeue
routes behind the bearer check. Splitting them is what makes "is this route
authenticated?" answerable by looking at which router it is registered on, rather
than by auditing each handler.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any, Final, cast

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import BodyTooLarge, NotFound, Unauthorized
from .job import Job, JobId, NewJob
from .state import AppState, get_state

__all__ = [
    "MAX_BODY_BYTES",
    "BodyLimitMiddleware",
    "bearer_matches",
    "protected_router",
    "public_router",
]

DEFAULT_DLQ_LIMIT: Final = 50
"""Page size for `GET /dlq` when the caller doesn't ask for one."""

MAX_DLQ_LIMIT: Final = 200
"""Hard ceiling on `GET /dlq`'s page size, so an unbounded DLQ can never be pulled
into one response (the "cap everything the caller controls" rule)."""

MAX_BODY_BYTES: Final = 256 * 1024
"""Coarse outer guard on the request body, well above the 64 KiB payload cap in
`job.NewJob`. Rejecting on `content-length` refuses an oversized body *before* it is
read and parsed, which is the point — the model's cap can only fire after the bytes
have already arrived."""


def bearer_matches(auth_header: str | None, expected: str) -> bool:
    """True iff `auth_header` is exactly `Bearer <token>` matching `expected`.

    The comparison is `hmac.compare_digest`, not `==`. A plain `==` on `str` returns
    as soon as two bytes differ, so the time it takes leaks how long a shared prefix
    the attacker guessed — enough to recover the token byte by byte. `compare_digest`
    is the stdlib's constant-time comparison and is the reason this needs no
    third-party dependency.

    An empty `expected` never matches: `Bearer ` with nothing after it must not
    authenticate, or a misconfigured deployment with a blank token would silently
    accept every request that bothered to send the header.
    """
    if not expected or auth_header is None:
        return False
    scheme, _, provided = auth_header.partition(" ")
    if scheme != "Bearer" or not provided:
        return False
    return hmac.compare_digest(provided, expected)


async def require_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Gate the mutating routes on `Authorization: Bearer <token>`.

    When no token is configured the check is skipped and `main` warns loudly at
    startup — with the `exec`/`shell` job kinds an open `POST /jobs` is remote code
    execution on every worker.
    """
    expected = get_state(request).enqueue_token
    if expected is None:
        return
    if not bearer_matches(authorization, expected):
        raise Unauthorized()


class BodyLimitMiddleware:
    """Refuse an oversized body **before** it is read and parsed.

    Written as raw ASGI, and a route dependency would not do. FastAPI reads and
    JSON-parses the request body *before* it solves a route's dependencies, so a
    `content-length` check expressed as a dependency runs too late to prevent
    anything — an enormous body would already have been buffered and parsed, and
    the caller would get a JSON-decode 400 rather than a 413.

    Checking `content-length` is a guard, not a guarantee: a chunked request sends
    no such header, and a lying one is possible. It is the cheap outer bound; the
    real semantic cap on what gets *stored* is `MAX_PAYLOAD_BYTES` in `job.py`.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
            raw = next((v for k, v in headers if k == b"content-length"), None)
            if raw is not None and raw.isdigit() and int(raw) > self.max_bytes:
                too_large = BodyTooLarge()
                response = JSONResponse(
                    status_code=too_large.status_code, content={"error": too_large.message}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


public_router = APIRouter()
protected_router = APIRouter(dependencies=[Depends(require_auth)])


@public_router.get("/healthz")
async def healthz() -> str:
    return "ok"


@public_router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: JobId) -> Job:
    state: AppState = get_state(request)
    job = await state.queue.get(job_id)
    if job is None:
        raise NotFound()
    return job


@public_router.get("/dlq")
async def get_dlq(
    request: Request,
    limit: Annotated[int, Query()] = DEFAULT_DLQ_LIMIT,
    offset: Annotated[int, Query()] = 0,
) -> list[Job]:
    """List dead-lettered jobs, newest first.

    `limit` is **clamped** into `[1, MAX_DLQ_LIMIT]` rather than rejected: an admin
    page asking for 1,000 rows should get the biggest page allowed, not a 400.
    """
    state: AppState = get_state(request)
    clamped = min(max(limit, 1), MAX_DLQ_LIMIT)
    return await state.queue.get_dlq(clamped, max(offset, 0))


@public_router.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    """Depth per state for the configured queue — what a dashboard polls."""
    state: AppState = get_state(request)
    counts = await state.queue.count_by_state(state.settings.queue)
    return {"queue": state.settings.queue, "counts": counts}


@protected_router.post("/jobs", status_code=201)
async def enqueue(request: Request, new: NewJob) -> dict[str, JobId]:
    """Enqueue a job.

    `new` is already validated by the time this runs — every cap the caller could
    exceed lives in :class:`~job_queue.job.NewJob`, and `errors` turns a violation
    into a 400.
    """
    state: AppState = get_state(request)
    job_id = await state.queue.enqueue(new)
    return {"id": job_id}


@protected_router.post("/job/{job_id}/requeue")
async def requeue_job(request: Request, job_id: JobId) -> Job:
    """Return a dead job to `ready`. 404 for anything that isn't dead."""
    state: AppState = get_state(request)
    job = await state.queue.requeue(job_id)
    if job is None:
        raise NotFound()
    return job
