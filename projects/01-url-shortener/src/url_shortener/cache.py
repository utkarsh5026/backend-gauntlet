"""V2 - Cache layer (cache-aside + stampede protection).

The redirect path is read-heavy and must mostly avoid Postgres. This wraps a
Redis client and holds the in-process single-flight state that keeps a herd of
requests for one just-expired key from all rebuilding it at once.

**Single-flight, the Python way.** The Rust version needed a `Mutex<HashMap<..>>`
plus a `Notify` per slot: real threads meant the check ("is someone already
rebuilding?") and the insert ("then I am the leader") could interleave, so they
had to happen inside a lock. Here they cannot interleave, because there is no
`await` between them - and a coroutine only yields at an `await`. So the whole
mutex disappears and what is left is a plain `dict[str, asyncio.Future]`: the
leader puts a future in, does the rebuild, and resolves it; everyone else finds
the future and parks on it. One Postgres query per herd instead of one per
request, in about fifteen lines.

Two details that are easy to get wrong and are worth reading twice:

* Waiters park with `asyncio.shield`, never a bare `await future`. Awaiting a
  future directly means *cancelling the waiter cancels the future* - so one
  client hanging up would blow up every other request parked on the same slot.
* The leader resolves the future from a `finally`-shaped path, so a rebuild that
  raises (or is cancelled) still wakes the waiters instead of stranding them.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, cast

from redis.asyncio import BlockingConnectionPool, Redis
from redis.exceptions import RedisError

__all__ = [
    "CACHE_ERRORS",
    "MISSING",
    "REDIS_POOL_MAX",
    "REDIS_POOL_TIMEOUT",
    "Cache",
    "CachePayloadError",
    "Cached",
    "Found",
    "Missing",
    "create_redis",
]

REDIS_POOL_MAX: Final[int] = 64
"""Connections to Redis. Bounded on purpose (the "bounded pool sized on purpose"
checklist item): each one is a socket and a client buffer on the Redis side, and
Redis is single-threaded, so past a point more connections buy latency, not
throughput."""

REDIS_POOL_TIMEOUT: Final[float] = 2.0
"""Seconds a request will wait for a free connection before giving up."""


def create_redis(
    url: str,
    *,
    max_connections: int = REDIS_POOL_MAX,
    timeout: float = REDIS_POOL_TIMEOUT,
) -> Redis:
    """The shared Redis client, on a **blocking** bounded pool.

    The pool choice is load-bearing, and the thundering herd is what exposes it.
    redis-py's default `ConnectionPool` *raises* `MaxConnectionsError` the moment
    it is exhausted - and that error is a `RedisError`, which the read path
    treats as "the cache is unwell, fall through to Postgres". So the exact
    moment a thousand clients arrive for one hot key, the cache would stop
    answering and send all thousand of them to the database: precisely the
    stampede the design exists to prevent, triggered by the load it exists to
    survive.

    `BlockingConnectionPool` queues for a free connection instead, with a
    timeout. Waiting 200 microseconds for a socket is the right answer; falling
    back to Postgres is not.

    `decode_responses=True` hands back `str`, so the codec never deals in bytes.
    The client is built eagerly but dials lazily - redis-py connects on the first
    command, not here.
    """
    pool = BlockingConnectionPool.from_url(  # pyright: ignore[reportUnknownMemberType]
        url,
        decode_responses=True,
        max_connections=max_connections,
        timeout=timeout,
    )
    return Redis(connection_pool=pool)


@dataclass(frozen=True, slots=True)
class Found:
    """A positive hit: the slug resolves to a live link."""

    link_id: int
    long_url: str


class Missing:
    """The slug is known *not* to exist - the negative cache entry.

    A singleton (:data:`MISSING`) rather than `None`, because "we looked and
    there is nothing" and "we have not looked" are different answers and the
    read path branches on which one it got.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"


MISSING: Final[Missing] = Missing()

type Cached = Found | Missing
"""What we store against a slug: a resolved link, or a remembered absence."""


class CachePayloadError(ValueError):
    """A cached value could not be decoded.

    Treated exactly like a Redis failure by the read path: the cache is not
    telling us anything usable, so fall through to Postgres.
    """


CACHE_ERRORS: Final[tuple[type[Exception], ...]] = (RedisError, CachePayloadError)
"""Everything the read path is willing to degrade past. See `resolve.py`."""


def encode(value: Cached) -> str:
    """Serialize a cache entry.

    The tagged shape (`{"Found": {...}}` / `"Missing"`) is deliberate: a bare
    object for the found case and `null` for the missing one would make an
    absent key and a negative entry decode to the same thing, which is precisely
    the distinction negative caching exists to draw.
    """
    if isinstance(value, Found):
        return json.dumps({"Found": {"link_id": value.link_id, "long_url": value.long_url}})
    return json.dumps("Missing")


def decode(payload: str) -> Cached:
    """Inverse of :func:`encode`.

    Raises:
        CachePayloadError: on anything that is not a value we wrote. Redis is
            shared infrastructure and keys outlive deploys, so a payload from an
            older schema is a real possibility, not a hypothetical.
    """
    try:
        parsed: object = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CachePayloadError(f"invalid cache payload: {exc}") from exc

    if parsed == "Missing":
        return MISSING
    if isinstance(parsed, dict):
        entry = cast("dict[str, object]", parsed).get("Found")
        if isinstance(entry, dict):
            fields = cast("dict[str, object]", entry)
            link_id = fields.get("link_id")
            long_url = fields.get("long_url")
            if isinstance(link_id, int) and isinstance(long_url, str):
                return Found(link_id=link_id, long_url=long_url)
    raise CachePayloadError("invalid cache payload: unrecognised shape")


class Cache:
    """Cache-aside wrapper over Redis, plus in-process single-flight state.

    One instance per process, shared by every handler: the Redis client holds a
    connection pool, and the single-flight dict only coalesces requests that
    share it.
    """

    FOUND_TTL_SECS: Final[int] = 3600
    MISSING_TTL_SECS: Final[int] = 60
    FOUND_TTL_JITTER_SECS: Final[int] = 300

    __slots__ = ("_inflight", "_redis", "key_prefix")

    def __init__(self, redis: Redis, key_prefix: str = "") -> None:
        """Wrap `redis`. `key_prefix` scopes every key - tests use it to isolate."""
        self._redis = redis
        self.key_prefix = key_prefix
        self._inflight: dict[str, asyncio.Future[Cached]] = {}

    def key(self, slug: str) -> str:
        return f"{self.key_prefix}link:{slug}"

    def _found_ttl(self) -> int:
        """Base TTL plus up to five minutes of jitter.

        Without the jitter, a batch of entries written together expires together,
        and one synchronized wave of misses hits Postgres at the same instant -
        a stampede you *scheduled*, on top of the one you cannot control.
        """
        return self.FOUND_TTL_SECS + random.randrange(self.FOUND_TTL_JITTER_SECS)

    async def get(self, slug: str) -> Cached | None:
        """Look a slug up. `None` means the key is absent - a real miss.

        Raises:
            RedisError: the command failed.
            CachePayloadError: the stored value is not decodable.
        """
        raw: Any = await self._redis.get(self.key(slug))
        if raw is None:
            return None
        if isinstance(raw, bytes | bytearray):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):  # pragma: no cover - client returns str/bytes
            raise CachePayloadError(f"unexpected cache value type: {type(raw)!r}")
        return decode(raw)

    async def put_found(self, slug: str, link_id: int, long_url: str) -> None:
        """Store a positive entry with a jittered TTL."""
        payload = encode(Found(link_id=link_id, long_url=long_url))
        await self._redis.set(self.key(slug), payload, ex=self._found_ttl())

    async def put_missing(self, slug: str) -> None:
        """Store a negative entry with a short, deliberately un-jittered TTL.

        Short because it is also the window in which a slug created *after* the
        404 stays invisible. Sixty seconds is the price paid for not letting a
        404 flood reach the database.
        """
        await self._redis.set(self.key(slug), encode(MISSING), ex=self.MISSING_TTL_SECS)

    async def delete(self, slug: str) -> None:
        """Drop an entry. Used by tests and by the create path to void a
        negative entry the moment the slug becomes real."""
        await self._redis.delete(self.key(slug))

    async def get_or_rebuild(self, slug: str, rebuild: Callable[[], Awaitable[Cached]]) -> Cached:
        """Read `slug`, rebuilding through `rebuild` on a miss - once per herd.

        A cache hit skips `rebuild` entirely. On a miss the first caller becomes
        the leader and runs it; everyone else parks on the leader's future and
        gets the leader's answer. The value is written to Redis *before* the
        waiters are woken, so nobody wakes up and immediately misses again.

        A failed rebuild is not cached and the slot is torn down, so the next
        request becomes a fresh leader and retries rather than inheriting a
        permanent error.
        """
        cached = await self.get(slug)
        if cached is not None:
            return cached

        parked = self._inflight.get(slug)
        if parked is not None:
            # Shielded: if *this* request is cancelled, the leader's rebuild and
            # every other waiter must survive it.
            return await asyncio.shield(parked)

        # No `await` between the check above and the insert below, so no other
        # task can slip in and elect itself a second leader. That is the whole
        # mutex, and it is spelled "do not yield here".
        future: asyncio.Future[Cached] = asyncio.get_running_loop().create_future()
        self._inflight[slug] = future

        try:
            value = await rebuild()
            if isinstance(value, Found):
                await self.put_found(slug, value.link_id, value.long_url)
            else:
                await self.put_missing(slug)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
                # Mark it retrieved. With no waiters parked, nothing would ever
                # call `.exception()` and asyncio would log a spurious
                # "Future exception was never retrieved" at garbage-collection.
                future.exception()
            raise
        else:
            future.set_result(value)
            return value
        finally:
            self._inflight.pop(slug, None)
