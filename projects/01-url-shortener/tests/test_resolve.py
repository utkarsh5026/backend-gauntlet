"""V2 - the cache-aside read path, end to end against Redis + Postgres.

These are the tests that pin the *policy*: what wins when the cache and the
database disagree, what gets written back, and what happens when Redis is
unwell. Driven through `resolve_slug` directly rather than over HTTP, so an
assertion failure points at the policy and not at the wiring.
"""

from __future__ import annotations

import asyncpg
import pytest
from redis.asyncio import Redis

from url_shortener.cache import MISSING, Found
from url_shortener.resolve import CacheOutcome, resolve_slug
from url_shortener.state import AppState

from .conftest import unique_slug


async def _seed_link(pool: asyncpg.Pool[asyncpg.Record], slug: str, url: str) -> int:
    link_id = abs(hash(slug)) % (2**62)
    await pool.execute(
        "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)", link_id, slug, url
    )
    return link_id


def test_outcome_labels_and_headers_agree() -> None:
    for outcome in CacheOutcome:
        assert outcome.header == outcome.label.upper()
        assert outcome.served_from


async def test_a_hit_never_reaches_postgres(app_state: AppState) -> None:
    """The whole point of the cache. Proven by the *absence* of a database row:
    if Postgres were consulted, this slug would resolve to nothing."""
    slug = unique_slug("hit")
    await app_state.cache.put_found(slug, 7, "https://cached.example.com/x")

    resolved = await resolve_slug(app_state, slug)

    assert resolved.outcome is CacheOutcome.HIT
    assert resolved.link == (7, "https://cached.example.com/x")


async def test_a_negative_entry_short_circuits_a_live_row(app_state: AppState) -> None:
    """A cached absence wins over a row that exists - which is exactly what
    negative caching means, and why its TTL is kept short."""
    slug = unique_slug("neg")
    await _seed_link(app_state.pool, slug, "https://real.example.com")
    await app_state.cache.put_missing(slug)

    resolved = await resolve_slug(app_state, slug)

    assert resolved.outcome is CacheOutcome.NEGATIVE
    assert resolved.link is None


async def test_a_miss_reads_postgres_and_back_fills(app_state: AppState) -> None:
    slug = unique_slug("miss")
    link_id = await _seed_link(app_state.pool, slug, "https://example.com/dest")

    resolved = await resolve_slug(app_state, slug)

    assert resolved.outcome is CacheOutcome.MISS
    assert resolved.link == (link_id, "https://example.com/dest")
    # The miss warmed the cache, so the next read is a hit.
    assert await app_state.cache.get(slug) == Found(link_id, "https://example.com/dest")


async def test_a_miss_on_an_absent_row_back_fills_a_negative(app_state: AppState) -> None:
    """What stops a 404 flood from hitting the database once per request."""
    slug = unique_slug("absent")

    resolved = await resolve_slug(app_state, slug)

    assert resolved.outcome is CacheOutcome.MISS
    assert resolved.link is None
    assert await app_state.cache.get(slug) is MISSING


async def test_a_broken_cache_degrades_instead_of_failing(
    app_state: AppState, redis_client: Redis
) -> None:
    """SPEC V2's "degrade, not die".

    An undecodable payload drives the same `Cache.get() -> raise` path a real
    outage would, and the observable contract is identical: the redirect still
    resolves, from Postgres, and nothing is written back - so the corrupt value
    is still there afterwards rather than being quietly papered over.
    """
    slug = unique_slug("degrade")
    link_id = await _seed_link(app_state.pool, slug, "https://example.com/survives")
    await redis_client.set(app_state.cache.key(slug), "{ not-valid-json", ex=60)

    resolved = await resolve_slug(app_state, slug)

    assert resolved.outcome is CacheOutcome.DEGRADED
    assert resolved.link == (link_id, "https://example.com/survives")
    assert await redis_client.get(app_state.cache.key(slug)) == "{ not-valid-json"


async def test_postgres_failure_still_propagates(app_state: AppState) -> None:
    """Degrading is for the cache only. With no database there is genuinely no
    answer, and pretending otherwise would serve a wrong 404."""
    await app_state.pool.close()
    with pytest.raises((asyncpg.PostgresError, asyncpg.InterfaceError, RuntimeError)):
        await resolve_slug(app_state, unique_slug("dead"))
