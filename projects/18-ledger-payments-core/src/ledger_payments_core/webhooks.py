"""V4 — Signed webhooks with retries, via the **transactional outbox**.

When a transfer settles, the merchant's endpoint must hear about it — and that
endpoint may be down, slow, or hostile. Three things must all hold:

1. **Atomic with the money.** The event is enqueued in the *same database
   transaction* as the posting (`webhook_outbox`), so a posting and its event commit
   together or not at all. That's why we don't fire the HTTP call after commit: a
   crash in that gap loses the event with no trace — and "enqueue to Redis after
   commit" is the same gap with a different second system.
2. **Signed.** Each delivery carries `X-Signature = HMAC-SHA256(secret, "{ts}.{body}")`
   and `X-Timestamp`, so the receiver can verify it's really you and refuse a replay.
   The secret is never logged; verification is a constant-time compare.
3. **At-least-once with backoff.** A failed delivery retries with exponential backoff
   plus jitter up to a cap, then dead-letters. Down ≠ lost — and because delivery is
   at-least-once, the *receiver* has to be idempotent too (V3, from the other side).

The dispatcher is a background task (gated by `RUN_DISPATCHER`) that claims due rows
with `FOR UPDATE SKIP LOCKED`, delivers them, and reschedules or dead-letters. Because
its state lives in Postgres, delivery survives a restart.

## Python notes

* **Hold the claim, not the connection.** A `FOR UPDATE` lock lasts as long as its
  transaction. Claim, then deliver over HTTP *inside* that transaction, and every
  in-flight delivery pins a pool connection for up to `timeout_secs` — the transfer
  path starves for connections because a merchant is slow. Decide how a claim outlives
  the transaction that made it before you write the loop.
* **Bound a round in time, not just in count.** Shutdown lets the dispatcher finish
  the round it's on (see `dispatch_loop`). Fifty sequential deliveries at a five-second
  timeout is four minutes — far past any grace period. Deliver concurrently under a
  bound (an `asyncio.TaskGroup` gated by an `asyncio.Semaphore`) and a round's worst
  case is closer to one timeout than to fifty.
* `hmac.compare_digest`, never `==`: string equality returns at the first differing
  byte, and that timing is an oracle for forging a signature one byte at a time.

## Proof (see SPEC V4)

* `sign`/`verify` round-trip; a tampered body fails; a stale timestamp fails.
* `backoff` is capped, jittered, and grows in expectation — a hypothesis test over a
  seeded `random.Random`.
* A receiver that always answers 500: attempts climb, `next_attempt_at` spreads out,
  then a `dead` row after `max_attempts` — never a tight loop.
* Roll back the posting's transaction and BOTH the entries and the outbox row are gone.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from uuid import UUID

import asyncpg
import httpx
import structlog
from pydantic import JsonValue, SecretStr

from .db import Conn
from .state import wait_for_shutdown

__all__ = [
    "SETTLED_EVENT",
    "SIGNATURE_HEADER",
    "SIGNATURE_TOLERANCE_SECS",
    "TIMESTAMP_HEADER",
    "WebhookConfig",
    "backoff",
    "dispatch_loop",
    "dispatch_once",
    "enqueue_settled",
    "sign",
    "verify",
]

logger = structlog.get_logger(__name__)

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"
SETTLED_EVENT = "transfer.settled"

SIGNATURE_TOLERANCE_SECS = 300
"""How far a delivery's timestamp may sit from the receiver's clock before `verify`
treats it as a replay. Stripe's default is the same five minutes."""


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    """Dispatcher tuning, built from `Settings` in `main`.

    `signing_secret` stays a `SecretStr` all the way down, so this dataclass's generated
    `repr` — the one an accidental `logger.info("…", cfg=cfg)` would print — shows the
    mask. Unwrap it only at the `sign` call.
    """

    signing_secret: SecretStr
    endpoint_url: str
    max_attempts: int
    dispatch_interval_secs: float
    dispatch_batch: int
    timeout_secs: float


async def enqueue_settled(
    conn: Conn, *, endpoint_url: str, transaction_id: UUID, payload: JsonValue
) -> None:
    """Enqueue a settlement event **on the posting's own connection and transaction**.

    TODO(V4): INSERT a `webhook_outbox` row (`event_type = SETTLED_EVENT`, the payload
    as JSONB, `endpoint_url`, `state = 'pending'`, `next_attempt_at = now()`) on `conn`
    — NOT on the pool, which would run it on whichever connection is free and commit it
    on its own: atomicity gone. Don't deliver here; this runs inside a money
    transaction, and the dispatcher delivers out of band. Put `transaction_id` in the
    payload: it is the receiver's idempotency key.
    """
    raise NotImplementedError("V4: INSERT the settlement event into webhook_outbox on conn")


def sign(secret: bytes, timestamp: int, body: bytes) -> str:
    """The delivery signature: `HMAC-SHA256(secret, "{timestamp}.{body}")`, hex.

    TODO(V4): `hmac.new(secret, message, hashlib.sha256).hexdigest()`, where `message`
    is the timestamp, a `.`, and the body — as *bytes*, exactly the bytes you POST. The
    timestamp is inside the MAC so a captured delivery can't be re-sent later under a
    fresh `X-Timestamp`. Sign the serialized body you send, never a dict you serialize
    again afterwards: one different space and no receiver can verify you.
    """
    raise NotImplementedError("V4: HMAC-SHA256 over (timestamp, body), hex-encoded")


def verify(
    secret: bytes,
    timestamp: int,
    body: bytes,
    signature: str,
    *,
    now: float | None = None,
    tolerance_secs: int = SIGNATURE_TOLERANCE_SECS,
) -> bool:
    """Verify a delivery signature in **constant time** — the receiver's half.

    TODO(V4): recompute `sign` and compare with `hmac.compare_digest`, never `==`. Also
    reject a `timestamp` more than `tolerance_secs` away from `now` (default: the
    current `time.time()`) — that check is what makes the timestamp replay protection.
    `now` is injectable so the stale-timestamp test needs no sleeping.
    """
    raise NotImplementedError("V4: constant-time verify of the signature + a timestamp check")


def backoff(
    attempt: int,
    *,
    base_secs: float = 1.0,
    cap_secs: float = 3600.0,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before retry `attempt` (1-based): exponential, capped, jittered.

    TODO(V4): something like `min(cap_secs, base_secs * 2 ** (attempt - 1))`, then
    jitter it with `rng` so deliveries that failed together don't retry together.
    "Full jitter" — a uniform draw between zero and that ceiling — is the well-known
    choice; say why you picked yours. `rng` is injectable so a property test can seed
    it. Mind the exponent: `2 ** 2000` is a perfectly good Python `int` that no `float`
    can hold, so cap *before* you multiply.
    """
    raise NotImplementedError("V4: exponential backoff with a cap and jitter")


async def dispatch_once(
    pool: asyncpg.Pool[asyncpg.Record], http: httpx.AsyncClient, cfg: WebhookConfig
) -> int:
    """One dispatch round. Returns how many events it settled — delivered, rescheduled,
    or dead-lettered.

    TODO(V4):
      1. Claim a batch of due events —
             SELECT … FROM webhook_outbox
             WHERE state = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at
             LIMIT $1
             FOR UPDATE SKIP LOCKED
         (SKIP LOCKED, so two dispatchers never grab the same event) — and read "Hold
         the claim, not the connection" in the module docs before deciding how long
         that transaction lives.
      2. For each: serialize the payload once, `sign` those bytes, and POST exactly
         them with `SIGNATURE_HEADER` / `TIMESTAMP_HEADER`.
           - 2xx  → `state = 'delivered'`
           - else → `attempts += 1` and record `last_error`; at `max_attempts`,
                    `state = 'dead'` (the DLQ), otherwise
                    `next_attempt_at = now() + backoff(attempts)`.
         A timeout or a refused connection is a failed attempt, not a crash of the loop.
      3. Count each outcome in `WEBHOOK_DELIVERIES_TOTAL`, and set
         `WEBHOOK_OUTBOX_LAG_SECONDS` from the oldest still-pending row.
    """
    raise NotImplementedError(
        "V4: claim due outbox events (FOR UPDATE SKIP LOCKED), sign, deliver, reschedule or DLQ"
    )


async def dispatch_loop(
    pool: asyncpg.Pool[asyncpg.Record],
    http: httpx.AsyncClient,
    cfg: WebhookConfig,
    shutdown: asyncio.Event,
) -> None:
    """The dispatcher: dispatch rounds until shutdown. Wired — the rounds are V4.

    Shutdown is checked *between* rounds, never inside one, so SIGTERM lets the current
    round settle before the task returns: the "no half-delivered state" item, provided
    a round is short (see the module docs). A round that filled its batch goes again
    straight away, so a backlog drains at the speed of delivery rather than one batch
    per interval; a partial round waits out the interval, waking early on shutdown.
    """
    logger.info("webhook dispatcher started", endpoint=cfg.endpoint_url, batch=cfg.dispatch_batch)
    while not shutdown.is_set():
        settled = await dispatch_once(pool, http, cfg)
        if settled >= cfg.dispatch_batch:
            continue
        if await wait_for_shutdown(shutdown, cfg.dispatch_interval_secs):
            break
    logger.info("webhook dispatcher stopped")
