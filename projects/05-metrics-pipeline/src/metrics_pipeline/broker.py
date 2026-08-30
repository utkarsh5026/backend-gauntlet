"""Broker wiring — the durable hop between ingest and the consumer pipeline.

This is glue, **not** a vertical: NATS JetStream is a dependency, and it gives
the pipeline a durable, replayable log so a `202`-accepted point survives a
consumer restart — the same decoupling Kafka buys. The at-least-once *semantics*
you reason about (ack-after-write, redelivery, dedup) are V3, in `sink.py` and
`pipeline.py`; this file just opens the pipe.

`nats-py` is pure Python and speaks the same JetStream primitives the verticals
care about: a durable stream, a pull consumer with explicit acks, and redelivery
of anything left unacked. Swapping in `aiokafka` later would change the imports
in this file and nothing else — which is the point of keeping it here.
"""

from __future__ import annotations

import asyncio

import nats
import structlog
from nats.aio.client import Client
from nats.js import JetStreamContext
from nats.js.api import StreamConfig
from nats.js.errors import NotFoundError

from .errors import BrokerUnavailable

__all__ = ["RAW_SUBJECT", "Producer", "connect", "ensure_stream"]

log = structlog.get_logger(__name__)

RAW_SUBJECT = "metrics.raw"
"""The subject (~ Kafka topic) raw points are published to."""


async def connect(url: str, deadline: float = 5.0) -> Client:
    """Open a client connection to NATS, giving up after `deadline` seconds.

    Two different retry policies are at work here and conflating them is a real
    outage shape, so they are separated on purpose:

    * **After** a successful connect, `max_reconnect_attempts=-1` means the
      client reconnects forever in the background. A broker restart should show
      up as a gap in ingest, not as a dead process.
    * **During** the initial connect, that same setting is a trap: `nats.connect`
      applies the reconnect loop to the first attempt too, so an unreachable
      broker makes startup hang *forever* rather than fail. `asyncio.wait_for` is
      what bounds it — without it the app never finishes booting, never binds a
      port, and never gets to report that it is degraded.

    Raises:
        TimeoutError: the broker did not answer within `deadline`.
    """
    # `nats.connect` takes **options that the library leaves untyped, so pyright
    # strict can't prove the return type. The library is the gap, not this call.
    return await asyncio.wait_for(
        nats.connect(  # pyright: ignore[reportUnknownMemberType]
            url,
            connect_timeout=2,
            max_reconnect_attempts=-1,
            reconnect_time_wait=0.5,
            error_cb=_on_error,
        ),
        timeout=deadline,
    )


async def _on_error(exc: Exception) -> None:
    """Route the client's async errors into our structured log.

    Without this the library prints a raw traceback to stderr on every failed
    reconnect, which in JSON-log land is unparseable noise that buries the one
    line an operator actually needs.
    """
    log.warning("nats connection error", error=str(exc))


async def ensure_stream(js: JetStreamContext, name: str) -> None:
    """Ensure the durable stream backing `RAW_SUBJECT` exists.

    Idempotent — safe to call on every startup. Both the producer (so publishes
    land) and the consumer (so it has something to bind to) depend on it.
    """
    try:
        await js.stream_info(name)
    except NotFoundError:
        # Same untyped-**params gap as `connect` above; the StreamConfig we pass
        # is fully typed.
        await js.add_stream(  # pyright: ignore[reportUnknownMemberType]
            StreamConfig(name=name, subjects=[RAW_SUBJECT])
        )
        log.info("created jetstream stream", stream=name, subject=RAW_SUBJECT)


class Producer:
    """Publishes raw line-protocol bytes to the durable stream.

    Held in the app state and used by the ingest handler. `js` is `None` when the
    broker was unreachable at startup: publishing then fails with a 503 rather
    than a 500, and the process still serves `/healthz` and `/metrics` so you can
    see *that* it is degraded. That split — liveness answers, the dependent path
    refuses — is the shape you want in anything that runs under an orchestrator.
    """

    def __init__(self, js: JetStreamContext | None, subject: str = RAW_SUBJECT) -> None:
        self._js = js
        self._subject = subject

    @property
    def connected(self) -> bool:
        return self._js is not None

    async def publish(self, payload: bytes) -> None:
        """Publish one payload and wait for the broker's durability ack.

        Awaiting the ack is what makes `202 Accepted` honest: it means the points
        are in the log, not merely in a socket buffer on their way to it.
        """
        if self._js is None:
            raise BrokerUnavailable("not connected to the broker")
        try:
            await self._js.publish(self._subject, payload)
        except Exception as exc:  # noqa: BLE001 - any broker failure is a 503
            raise BrokerUnavailable(str(exc)) from exc
