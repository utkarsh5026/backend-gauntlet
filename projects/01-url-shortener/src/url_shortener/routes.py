"""HTTP routes.

Route order matters here. `GET /{slug}` is a single-segment catch-all, so every
fixed path (`/healthz`, `/assets/...`) has to be registered before it - Starlette
matches in registration order and the first match wins.

The write and stats endpoints hang off a router that carries the auth dependency,
so a request without a valid key is rejected before any handler body runs. The
public redirect is on a router that simply does not have it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import metrics
from .auth import require_api_key
from .errors import BadRequest, NotFound
from .id_gen import CUSTOM_EPOCH_MS, decode
from .ingest import ClickEvent
from .ratelimit import enforce_rate_limit
from .resolve import resolve_slug
from .state import AppState, get_state
from .url_validate import validate_long_url

__all__ = [
    "MAX_CUSTOM_SLUG_LEN",
    "MIN_CUSTOM_SLUG_LEN",
    "RESERVED_SLUGS",
    "dashboard_dist",
    "protected_router",
    "public_router",
    "validate_custom_slug",
]

log = structlog.get_logger(__name__)

MIN_CUSTOM_SLUG_LEN = 1
MAX_CUSTOM_SLUG_LEN = 64

RESERVED_SLUGS = frozenset({"healthz", "api", "assets", "metrics"})
"""Slugs that would shadow a first-class route. `/{slug}` is registered last, so
these would never actually be reached - which is exactly why claiming one has to
be refused at write time rather than discovered as a mystery 404 later."""

_UNIQUE_VIOLATION = "23505"

_INSERT_LINK = "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)"


def dashboard_dist() -> Path | None:
    """Locate the built dashboard (`dashboard/dist`), or `None` if unbuilt.

    Checked at startup rather than per request. Unlike the Rust build there is no
    embedding step - a Python service ships its data files alongside the code -
    so "the frontend was never built" is a normal state that has to degrade to a
    404 instead of a crash.
    """
    candidates = (
        Path.cwd() / "dashboard" / "dist",
        Path(__file__).resolve().parents[2] / "dashboard" / "dist",
    )
    return next((path for path in candidates if (path / "index.html").is_file()), None)


def validate_custom_slug(raw: str) -> str:
    """Validate a user-supplied vanity slug.

    Uniqueness is *not* checked here - that would be a check-then-insert race
    against every other concurrent request. The unique index on `links.slug` is
    the real arbiter; a duplicate surfaces as a constraint violation at insert
    time and is translated there.

    Raises:
        BadRequest: empty, too long, illegal characters, or reserved.
    """
    slug = raw.strip()
    if not MIN_CUSTOM_SLUG_LEN <= len(slug) <= MAX_CUSTOM_SLUG_LEN:
        raise BadRequest(
            f"slug length must be {MIN_CUSTOM_SLUG_LEN}-{MAX_CUSTOM_SLUG_LEN} characters"
        )
    if not all(char.isascii() and (char.isalnum() or char in "-_") for char in slug):
        raise BadRequest("slug may only contain letters, numbers, hyphens, and underscores")
    if slug.lower() in RESERVED_SLUGS:
        raise BadRequest("slug is reserved")
    return slug


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class CreateLink(BaseModel):
    url: str
    custom_slug: str | None = None


class CreatedLink(BaseModel):
    slug: str
    short_url: str
    long_url: str


class LinkStats(BaseModel):
    slug: str
    long_url: str
    total_clicks: int


class SnowflakeParts(BaseModel):
    id: int = Field(description="The raw 64-bit id (the slug is this number, base62-encoded)")
    timestamp_unix_ms: int = Field(description="Milliseconds since the Unix epoch")
    custom_epoch_unix_ms: int = Field(description="The epoch the timestamp bits count from")
    node_id: int = Field(description="Node id baked into the id - why two instances never collide")
    sequence: int = Field(description="Per-millisecond sequence counter")


class ResolveDebug(BaseModel):
    slug: str
    found: bool
    long_url: str | None
    cache: str
    served_from: str
    latency_ms: float
    snowflake: SnowflakeParts | None


# --------------------------------------------------------------------------- #
# Public routes
# --------------------------------------------------------------------------- #

public_router = APIRouter()


@public_router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@public_router.get("/", include_in_schema=False)
async def dashboard(request: Request) -> Response:
    """The demo SPA's entrypoint. 404s when the frontend has not been built."""
    dist: Path | None = request.app.state.dashboard_dist
    if dist is None:
        return Response(status_code=404)
    return FileResponse(dist / "index.html", media_type="text/html")


@public_router.get("/api/debug/resolve/{slug}")
async def resolve_debug(request: Request, slug: str) -> ResolveDebug:
    """Read-only inspector for the demo dashboard.

    Runs exactly the resolution a redirect would, but returns JSON describing the
    cache outcome and the decoded Snowflake instead of a 3xx. Public, like the
    redirect - and it deliberately does *not* record a click, so watching the
    dashboard cannot skew the analytics it is displaying.
    """
    state = get_state(request)
    started = time.perf_counter()
    resolved = await resolve_slug(state, slug)
    latency_ms = (time.perf_counter() - started) * 1000

    snowflake: SnowflakeParts | None = None
    if resolved.link is not None:
        link_id, _ = resolved.link
        parts = decode(link_id)
        snowflake = SnowflakeParts(
            id=link_id,
            timestamp_unix_ms=parts.timestamp_ms + CUSTOM_EPOCH_MS,
            custom_epoch_unix_ms=CUSTOM_EPOCH_MS,
            node_id=parts.node_id,
            sequence=parts.sequence,
        )

    return ResolveDebug(
        slug=slug,
        found=resolved.link is not None,
        long_url=resolved.link[1] if resolved.link is not None else None,
        cache=resolved.outcome.label,
        served_from=resolved.outcome.served_from,
        latency_ms=latency_ms,
        snowflake=snowflake,
    )


# --------------------------------------------------------------------------- #
# Protected routes (API key required)
# --------------------------------------------------------------------------- #

protected_router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


async def _rate_limit(
    request: Request, api_key: Annotated[str, Depends(require_api_key)]
) -> dict[str, str]:
    """Charge the caller's key. Depends on `require_api_key`, so the limiter can
    only ever see a token that has already been validated - and FastAPI's
    per-request dependency cache means the header is parsed once, not twice."""
    return await enforce_rate_limit(api_key, get_state(request).limiter)


@protected_router.post(
    "/links",
    status_code=201,
    dependencies=[Depends(_rate_limit)],
)
async def create_link(request: Request, body: CreateLink) -> CreatedLink:
    """Mint a slug for a submitted URL.

    The id comes from the in-process generator - no `INSERT ... RETURNING id`,
    no sequence, no round-trip before the write (V1).
    """
    state = get_state(request)
    long_url = validate_long_url(body.url)

    if body.custom_slug is not None:
        link_id = state.ids.next_id()
        slug = validate_custom_slug(body.custom_slug)
    else:
        link_id, slug = state.ids.next_id_and_slug()

    try:
        await state.pool.execute(_INSERT_LINK, link_id, slug, long_url)
    except asyncpg.UniqueViolationError as exc:
        raise BadRequest("slug already in use") from exc
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", None) == _UNIQUE_VIOLATION:
            raise BadRequest("slug already in use") from exc
        raise

    # A 404 on this slug may have been negatively cached moments ago (someone
    # probing for a vanity name). Dropping the entry now means the link works
    # immediately instead of after the negative TTL lapses.
    try:
        await state.cache.delete(slug)
    except Exception as exc:  # noqa: BLE001 - the link exists either way
        log.warning("could not clear negative cache entry", slug=slug, error=str(exc))

    return CreatedLink(
        slug=slug,
        short_url=f"{state.base_url}/{slug}",
        long_url=long_url,
    )


@protected_router.get("/links/{slug}/stats")
async def link_stats(request: Request, slug: str) -> LinkStats:
    """TODO(stats): aggregate click stats for the slug.

    Start with a `COUNT(*)` over `click_events`. Then think about what that query
    does once the table has millions of rows and the count has to scan every one
    of them - which is precisely the pressure that pushes this workload toward a
    columnar store in Tier 3, and why the answer is usually a maintained rollup
    rather than a faster count.
    """
    _ = (request, slug)
    raise NotImplementedError("implement stats aggregation")


# --------------------------------------------------------------------------- #
# The redirect - registered last, because `/{slug}` matches anything
# --------------------------------------------------------------------------- #

redirect_router = APIRouter()


@redirect_router.get("/{slug}", include_in_schema=False)
async def redirect(request: Request, slug: str) -> Response:
    """Cache-aside redirect: Redis first, Postgres on a miss.

    Emits one structured line per redirect carrying slug, cache outcome and
    latency - the three fields the observability checklist grades - and an
    `X-Cache` header so the same outcome is visible from curl without reading
    logs.
    """
    state = get_state(request)
    started = time.perf_counter()

    resolved = await resolve_slug(state, slug)
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    metrics.CACHE_LOOKUPS_TOTAL.labels(outcome=resolved.outcome.label).inc()

    if resolved.link is None:
        log.info("redirect missed", slug=slug, cache=resolved.outcome.label, latency_ms=latency_ms)
        raise NotFound

    link_id, long_url = resolved.link
    _record_click(state, link_id, request)
    metrics.REDIRECTS_TOTAL.labels(cache=resolved.outcome.label).inc()
    log.info("redirect served", slug=slug, cache=resolved.outcome.label, latency_ms=latency_ms)

    # 302, not 301. A permanent redirect is cached by browsers and proxies, so
    # repeat visits would never reach this server and every click after the first
    # would go uncounted. Analytics is the product here, so the redirect stays
    # temporary and `no-store` keeps intermediaries from second-guessing that.
    return Response(
        status_code=302,
        headers={
            "location": long_url,
            "x-cache": resolved.outcome.header,
            "cache-control": "no-store",
        },
    )


def _record_click(state: AppState, link_id: int, request: Request) -> None:
    """Hand the click off. Synchronous and fire-and-forget by construction:
    there is no `await` here, so the redirect cannot wait on ingestion."""
    state.clicks.accept(
        ClickEvent(
            link_id=link_id,
            referer=request.headers.get("referer"),
            user_agent=request.headers.get("user-agent"),
            ip_hash=None,
        )
    )
