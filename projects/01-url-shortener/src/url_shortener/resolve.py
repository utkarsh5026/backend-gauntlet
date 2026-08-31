"""V2 cache-aside slug resolution - the shared read path.

Redis first, Postgres on a miss, back-filling positive *and* negative entries.
Both the redirect and the debug inspector go through here, so the resolution
policy lives in one place and the demo's observability sees exactly what a real
redirect sees.

**Degrade, not die.** A Redis failure must not 500 a redirect. The cache read is
therefore caught rather than propagated, and the service falls through to
Postgres. When it does, the back-fill writes are skipped - if the cache is
unhealthy enough to fail a read, writing to it is not going to help, and doing so
risks papering a corrupt payload over with a fresh one before anyone notices.
Only a *Postgres* failure still propagates: at that point there is genuinely no
answer to give.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import structlog

from .cache import CACHE_ERRORS, Found
from .state import AppState

__all__ = ["CacheOutcome", "Resolved", "resolve_slug"]

log = structlog.get_logger(__name__)

_SELECT_LINK = "SELECT id, long_url FROM links WHERE slug = $1"


class CacheOutcome(Enum):
    """Where a resolution was ultimately served from.

    Exposed to clients as the `X-Cache` header and in the debug JSON, and used as
    the metric label - so the hit ratio on the dashboard and the header on a
    single curl are computed from the same value.
    """

    HIT = "hit"
    """Served straight from Redis (positive entry)."""

    NEGATIVE = "negative"
    """Redis held a negative entry - a 404 without touching Postgres."""

    MISS = "miss"
    """Redis missed; Postgres answered and the result was back-filled."""

    DEGRADED = "degraded"
    """The Redis read failed; Postgres answered and nothing was back-filled."""

    @property
    def label(self) -> str:
        """Lowercase form, for log fields and metric labels."""
        return self.value

    @property
    def header(self) -> str:
        """Uppercase form, for the `X-Cache` response header."""
        return self.value.upper()

    @property
    def served_from(self) -> str:
        """Human-readable store, for the debug inspector."""
        return _SERVED_FROM[self]


_SERVED_FROM: dict[CacheOutcome, str] = {
    CacheOutcome.HIT: "redis (cache hit)",
    CacheOutcome.NEGATIVE: "redis (negative cache)",
    CacheOutcome.MISS: "postgres (cache miss -> back-filled)",
    CacheOutcome.DEGRADED: "postgres (cache unavailable -> no back-fill)",
}


@dataclass(frozen=True, slots=True)
class Resolved:
    """The outcome of resolving one slug."""

    outcome: CacheOutcome
    link: tuple[int, str] | None
    """`(link_id, long_url)`, or `None` when the slug does not resolve."""


async def resolve_slug(state: AppState, slug: str) -> Resolved:
    """Resolve `slug` through the cache-aside path.

    Raises:
        asyncpg.PostgresError: Postgres itself failed. Cache failures do not
            raise - they degrade.
    """
    degraded = False
    try:
        cached = await state.cache.get(slug)
    except CACHE_ERRORS as exc:
        log.warning("cache read failed; degrading to postgres", slug=slug, error=str(exc))
        degraded = True
    else:
        if isinstance(cached, Found):
            return Resolved(outcome=CacheOutcome.HIT, link=(cached.link_id, cached.long_url))
        if cached is not None:
            return Resolved(outcome=CacheOutcome.NEGATIVE, link=None)

    row = await state.pool.fetchrow(_SELECT_LINK, slug)

    if row is None:
        if not degraded:
            await _try_backfill_missing(state, slug)
        return Resolved(outcome=CacheOutcome.DEGRADED if degraded else CacheOutcome.MISS, link=None)

    link_id: int = row["id"]
    long_url: str = row["long_url"]
    if not degraded:
        await _try_backfill_found(state, slug, link_id, long_url)
    return Resolved(
        outcome=CacheOutcome.DEGRADED if degraded else CacheOutcome.MISS,
        link=(link_id, long_url),
    )


async def _try_backfill_found(state: AppState, slug: str, link_id: int, long_url: str) -> None:
    """Warm the cache, best-effort. A failed write must not fail the redirect -
    we already have the answer; the cache is an optimisation, not the source."""
    try:
        await state.cache.put_found(slug, link_id, long_url)
    except CACHE_ERRORS as exc:
        log.warning("cache back-fill failed", slug=slug, error=str(exc))


async def _try_backfill_missing(state: AppState, slug: str) -> None:
    """Remember the absence, best-effort - this is what stops a 404 flood from
    reaching Postgres once per request."""
    try:
        await state.cache.put_missing(slug)
    except CACHE_ERRORS as exc:
        log.warning("negative cache back-fill failed", slug=slug, error=str(exc))
