"""V2 - the cache codec (pure) and the cache itself (needs Redis).

The codec tests run everywhere. The rest use the `cache` fixture, which skips
when Redis is not up and scopes every key under its own prefix.
"""

from __future__ import annotations

import asyncio

import pytest

from url_shortener.cache import (
    MISSING,
    Cache,
    Cached,
    CachePayloadError,
    Found,
    decode,
    encode,
)

from .conftest import unique_slug


async def _settle() -> None:
    """Let every pending task reach its next await.

    The herd tests need waiters to actually be *parked on the leader's slot*
    before the leader is released - otherwise they arrive after it finished, each
    finds an empty slot, and each elects itself leader. That would fail the test
    without the code being wrong, which is the worst kind of test.
    """
    await asyncio.sleep(0.05)


async def _warm_pool(cache: Cache, connections: int) -> None:
    """Open `connections` Redis sockets before a herd test starts.

    Worth understanding, because it bit this suite. Every task in a herd begins
    with a cache read, and on a cold pool that read has to *establish a TCP
    connection* first - which is orders of magnitude slower than the GET itself.
    So the waiters showed up long after the leader had finished, each found an
    empty single-flight slot, and each elected itself leader.

    The success-path test hides this (the leader's cache write turns late
    arrivals into hits), which is exactly why the failure-path test caught it: a
    failed rebuild is deliberately not cached, so there is nothing to catch a
    straggler. Warming the pool makes the reads fast enough that arrival order is
    determined by the code under test rather than by socket setup.
    """
    await asyncio.gather(*(cache.get(f"warm-{index}") for index in range(connections)))


# --------------------------------------------------------------------------- #
# codec
# --------------------------------------------------------------------------- #


def test_found_round_trips() -> None:
    original = Found(link_id=42, long_url="https://example.com")
    assert decode(encode(original)) == original


def test_missing_round_trips() -> None:
    assert encode(MISSING) == '"Missing"'
    assert decode(encode(MISSING)) is MISSING


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "{ not-valid-json",
        "42",
        '{"Found": {}}',
        '{"Found": {"link_id": "not-an-int", "long_url": "x"}}',
        '{"Unknown": 1}',
    ],
)
def test_decode_rejects_anything_we_did_not_write(payload: str) -> None:
    """Redis is shared infrastructure and keys outlive deploys, so a payload from
    an older schema is a real possibility - it must degrade, not crash."""
    with pytest.raises(CachePayloadError):
        decode(payload)


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #


async def test_get_returns_none_for_an_unknown_slug(cache: Cache) -> None:
    """`None` (never looked) and `MISSING` (looked, nothing there) are different
    answers, and the read path branches on which one it got."""
    assert await cache.get(unique_slug("absent")) is None


async def test_put_found_then_get(cache: Cache) -> None:
    slug = unique_slug("found")
    await cache.put_found(slug, 99, "https://example.com/page")
    assert await cache.get(slug) == Found(link_id=99, long_url="https://example.com/page")


async def test_put_missing_then_get(cache: Cache) -> None:
    slug = unique_slug("missing")
    await cache.put_missing(slug)
    assert await cache.get(slug) is MISSING


async def test_found_ttl_carries_jitter(cache: Cache) -> None:
    """Entries written together must not all expire on the same tick - that is a
    stampede you scheduled yourself."""
    ttls: set[int] = set()
    for index in range(30):
        slug = unique_slug(f"ttl-{index}")
        await cache.put_found(slug, index, "https://example.com")
        ttls.add(await cache._redis.ttl(cache.key(slug)))  # pyright: ignore[reportPrivateUsage]

    assert len(ttls) > 1, "every TTL identical - the jitter is not being applied"
    assert all(
        Cache.FOUND_TTL_SECS <= ttl <= Cache.FOUND_TTL_SECS + Cache.FOUND_TTL_JITTER_SECS
        for ttl in ttls
    )


async def test_missing_ttl_is_short(cache: Cache) -> None:
    """Short, because it is also the window in which a slug created after the
    404 stays invisible."""
    slug = unique_slug("neg-ttl")
    await cache.put_missing(slug)
    ttl = await cache._redis.ttl(cache.key(slug))  # pyright: ignore[reportPrivateUsage]
    assert 0 < ttl <= Cache.MISSING_TTL_SECS


async def test_delete_removes_an_entry(cache: Cache) -> None:
    slug = unique_slug("del")
    await cache.put_missing(slug)
    await cache.delete(slug)
    assert await cache.get(slug) is None


# --------------------------------------------------------------------------- #
# single-flight (the stampede invariant)
# --------------------------------------------------------------------------- #


async def test_hit_skips_the_rebuild_entirely(cache: Cache) -> None:
    slug = unique_slug("sf-hit")
    await cache.put_found(slug, 7, "https://example.com/cached")
    calls = 0

    async def rebuild() -> Cached:
        nonlocal calls
        calls += 1
        return MISSING

    assert await cache.get_or_rebuild(slug, rebuild) == Found(7, "https://example.com/cached")
    assert calls == 0


async def test_miss_rebuilds_and_populates(cache: Cache) -> None:
    slug = unique_slug("sf-miss")

    async def rebuild() -> Cached:
        return Found(link_id=1, long_url="https://example.com/built")

    built = await cache.get_or_rebuild(slug, rebuild)
    assert built == Found(1, "https://example.com/built")
    # Written through before waiters wake, so the next read is a hit.
    assert await cache.get(slug) == built


async def test_negative_result_is_cached_too(cache: Cache) -> None:
    slug = unique_slug("sf-neg")

    async def rebuild() -> Cached:
        return MISSING

    assert await cache.get_or_rebuild(slug, rebuild) is MISSING
    assert await cache.get(slug) is MISSING


async def test_thousand_concurrent_misses_rebuild_once(cache: Cache) -> None:
    """**The SPEC's stampede invariant.**

    A thousand tasks race for the same cold slug. Exactly one rebuild may run -
    that rebuild is the Postgres query, and one-per-request is what takes the
    database down when a link hits the front page.
    """
    slug = unique_slug("herd")
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def rebuild() -> Cached:
        nonlocal calls
        calls += 1
        started.set()
        # Hold the leader open so every other task is guaranteed to arrive while
        # the rebuild is genuinely in flight, rather than after it finished.
        await release.wait()
        return Found(link_id=1, long_url="https://example.com/hot")

    await _warm_pool(cache, 64)
    herd = [asyncio.create_task(cache.get_or_rebuild(slug, rebuild)) for _ in range(1_000)]
    await started.wait()
    await _settle()
    release.set()
    results = await asyncio.gather(*herd)

    assert calls == 1, f"expected one rebuild for the herd, got {calls}"
    assert all(result == Found(1, "https://example.com/hot") for result in results)


async def test_a_failed_rebuild_reaches_every_waiter_and_is_not_cached(cache: Cache) -> None:
    """A failure must propagate to the whole herd *and* leave nothing behind, so
    the next request retries instead of inheriting a permanent error."""
    slug = unique_slug("sf-fail")
    release = asyncio.Event()
    calls = 0

    started = asyncio.Event()

    async def failing() -> Cached:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise RuntimeError("postgres is down")

    await _warm_pool(cache, 50)
    herd = [asyncio.create_task(cache.get_or_rebuild(slug, failing)) for _ in range(50)]
    await started.wait()
    await _settle()
    release.set()
    results = await asyncio.gather(*herd, return_exceptions=True)

    assert calls == 1
    assert all(isinstance(result, RuntimeError) for result in results)
    assert await cache.get(slug) is None, "a failed rebuild must not be cached"


async def test_retries_with_a_fresh_leader_after_a_failure(cache: Cache) -> None:
    slug = unique_slug("sf-retry")
    calls = 0

    async def flaky() -> Cached:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return Found(link_id=2, long_url="https://example.com/second")

    with pytest.raises(RuntimeError):
        await cache.get_or_rebuild(slug, flaky)

    assert await cache.get_or_rebuild(slug, flaky) == Found(2, "https://example.com/second")
    assert calls == 2


async def test_a_cancelled_waiter_does_not_break_the_herd(cache: Cache) -> None:
    """Why waiters park with `asyncio.shield`.

    A bare `await future` would mean cancelling one waiter cancels the shared
    future - so a single client hanging up would fail every other request parked
    on the same slug.
    """
    slug = unique_slug("sf-cancel")
    release = asyncio.Event()

    async def rebuild() -> Cached:
        await release.wait()
        return Found(link_id=3, long_url="https://example.com/survived")

    await _warm_pool(cache, 8)
    leader = asyncio.create_task(cache.get_or_rebuild(slug, rebuild))
    await _settle()
    waiters = [asyncio.create_task(cache.get_or_rebuild(slug, rebuild)) for _ in range(5)]
    await _settle()

    waiters[0].cancel()
    release.set()

    assert await leader == Found(3, "https://example.com/survived")
    for waiter in waiters[1:]:
        assert await waiter == Found(3, "https://example.com/survived")
