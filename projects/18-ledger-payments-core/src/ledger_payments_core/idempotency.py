"""V3 — Idempotency keys: exactly-once *effects* over an at-least-once network.

A client whose `POST /transfers` times out can't know whether the money moved, so it
retries — and without protection you've charged twice. The `Idempotency-Key` header
fixes it: the first request with a key does the work and **stores its response**; any
replay of that key returns the stored response without re-executing.

Storage is two-tier. **Redis** caches the response for fast replay; **Postgres**
(`idempotency_keys`) is the durable record. A cache miss re-reads Postgres and *still
never re-executes*. The record binds the key to a **fingerprint** of the request, so
the same key with a different body is a conflict — not a silent replay of some other
request's result.

## The shape: one call around the money

The route doesn't choreograph reserve → execute → store itself. It hands
`IdempotencyStore.run` the key, the fingerprint, and the transfer to execute, and gets
back the response to send — fresh or replayed. That's deliberate: *when* the result is
recorded relative to the money moving is the decision this vertical grades
(`docs/18-design.md`), and it can't be your decision if the route has already made it.
The callback is where you start, not a contract: if you conclude the record belongs
*inside* the transfer's own transaction, change the signatures to match.

## The three hard cases

* **Concurrent duplicates.** Two identical requests arrive before either finishes, and
  exactly one may execute. An `asyncio.Lock` per key is not an answer: it only
  serializes coroutines in *this* process, and the boss fight — like production — runs
  several. The lever has to live in the shared store: a reservation on the key's
  primary key that only one racer can win.
* **A failure after reserving.** The transfer raised after you reserved the key. A
  clean rejection (a `402`, a `400`) can be stored as the response — Stripe does. An
  *ambiguous* one — the connection died around `COMMIT` — leaves you not knowing
  whether the money moved, and freeing the key for a re-execution is precisely the
  double charge this module exists to prevent.
* **Expiry.** Past `ttl_secs` a key is new again. Too short and a late retry
  double-charges; too long and the table grows without bound.

## Proof (see SPEC V3)

* The same key twice: the second is a replay (same body, same transaction id) and the
  ledger holds exactly ONE transaction.
* The same key with a different body: `IdempotencyConflictError`, never a replay.
* A concurrent double-submit — `asyncio.gather` of two requests with the same key —
  produces exactly one posting.
* `FLUSHDB` on Redis, then replay: served from Postgres, and still not re-executed.
* An expired key executes as new.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, JsonValue
from redis.asyncio import Redis

__all__ = ["IdempotencyStore", "IdempotentResult", "StoredResponse", "fingerprint"]


class StoredResponse(BaseModel):
    """What a replay returns instead of re-executing: the whole response, not a
    "done" flag, because a replay has to be indistinguishable from the original.

    A pydantic model so the Redis round-trip is `model_dump_json()` /
    `model_validate_json()` — the same shape in the cache as in the row.
    """

    model_config = ConfigDict(frozen=True)

    status_code: int
    transaction_id: UUID | None = None
    body: JsonValue


@dataclass(frozen=True, slots=True)
class IdempotentResult:
    """What `IdempotencyStore.run` hands back to the route."""

    response: StoredResponse
    replayed: bool
    """`True` when `response` came from the store rather than from executing. The route
    turns an original `201` into a `200` for a replay."""


def fingerprint(body: bytes) -> str:
    """A stable fingerprint of a request body, so the same key with a different body
    is detectable as a conflict.

    TODO(V3): `hashlib.sha256(…).hexdigest()` — the question is *over which bytes*.
    Two JSON bodies that differ only in key order or whitespace are the same request,
    so hashing the raw bytes makes an innocent client re-serialization a conflict.
    Normalise first (parse, then `json.dumps(…, sort_keys=True, separators=(",", ":"))`),
    or decide that byte-exact is your contract and document it.
    """
    raise NotImplementedError("V3: a stable fingerprint of the request body")


class IdempotencyStore:
    """Two-tier idempotency store: a Redis cache in front of the Postgres record."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record], redis: Redis, *, ttl_secs: int) -> None:
        self._pool = pool
        self._redis = redis
        self._ttl_secs = ttl_secs

    async def run(
        self,
        key: str,
        request_hash: str,
        execute: Callable[[], Awaitable[StoredResponse]],
    ) -> IdempotentResult:
        """Run `execute` at most once per `key`, returning its response — or the one
        already stored.

        TODO(V3): the reserve-then-execute handshake.
          1. Fast path: `GET` the key from Redis. A hit whose `request_hash` matches is
             a replay (count an idempotency hit); a mismatch is
             `IdempotencyConflictError`.
          2. Miss: go to Postgres and try to *reserve* the key — insert (key,
             `request_hash`, no response yet, `expires_at = now() + ttl`) so that
             exactly one concurrent caller wins (`INSERT … ON CONFLICT DO NOTHING
             RETURNING` tells you whether it was you).
               - won:  `await execute()`, persist the response to the row, then `SET`
                       it in Redis with the same TTL. Durable record first, cache
                       second: a crash between them costs a cache miss, never the truth.
               - lost: read the row —
                   · hash differs            → `IdempotencyConflictError`
                   · response present       → replay it
                   · no response yet        → still in flight: wait and re-read, or
                                              raise `IdempotencyInProgressError`
                   · expired                → treat as new (your TTL policy)
          3. Decide what a raising `execute` does to the reservation — "A failure after
             reserving" in the module docs. Whatever you choose, a row must never be
             left looking in flight forever.
        Redis errors on the fast path degrade to Postgres; they don't fail the request.
        """
        raise NotImplementedError(
            "V3: check Redis, else reserve the key in Postgres, then execute at most once"
        )
