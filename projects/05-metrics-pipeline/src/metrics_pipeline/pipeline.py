"""The consumer pipeline: consume -> roll up -> batch-sink -> fan out. (Wiring.)

This is glue — it *drives* the verticals and ties them into a lifecycle. It pulls
raw lines off the durable stream, parses them (V1), folds them into the rollup
engine (V2), flushes closed windows to the batched ClickHouse sink (V3) and the
live SSE feed (V4), and acks the broker **only after** a batch is durably written
(at-least-once). With the verticals still raising, the loop dies on its first
real point — that traceback is the worklist. It runs only when
`RUN_CONSUMER=true` (see `main.py`), so the bare scaffold serves ingest cleanly.

**Why one task and not several.** The loop is a single coroutine that owns the
`Rollup` and the `Sink` outright. That is what lets both be plain synchronous
objects with no locks: there is exactly one writer. The alternative — a fan-out
of consumer tasks sharing a rollup map — buys you nothing on CPython (the GIL
means the aggregation itself never runs in parallel) and costs you every
read-modify-write race in the engine. If you later need more throughput, the
scaling axis is *processes* (one consumer per shard of the series space), not
tasks sharing state. Write that decision down; it is a design-doc item.

The fetch timeout doubles as the loop's clock: a quiet stream wakes up every
`flush_interval` anyway, which is exactly when the watermark and the time-based
batch trigger need to run.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from nats.aio.msg import Msg
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext

from . import parse
from .rollup import Rollup
from .sink import Sink
from .sse import LiveFeed

__all__ = ["PipelineConfig", "run"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class PipelineConfig:
    """Tuning for the consumer pipeline, assembled in `main.py`."""

    stream_name: str
    """JetStream stream to bind the durable consumer to."""
    subject: str
    durable_name: str
    """Durable consumer name — survives restarts so the offset is remembered."""
    fetch_batch: int
    """Messages pulled per fetch — the broker -> rollup prefetch bound."""
    flush_interval: float
    """Seconds between watermark flushes / time-based batch flushes."""


async def run(
    js: JetStreamContext,
    cfg: PipelineConfig,
    rollup: Rollup,
    sink: Sink,
    feed: LiveFeed,
    shutdown: asyncio.Event,
) -> None:
    """Run the pipeline until `shutdown` is set.

    Owns the `Rollup` and the `Sink` for its lifetime; `LiveFeed` is the shared
    hub, also held by the SSE handlers.
    """
    log.info("consumer pipeline starting", stream=cfg.stream_name, durable=cfg.durable_name)

    subscription = await js.pull_subscribe(
        cfg.subject,
        durable=cfg.durable_name,
        stream=cfg.stream_name,
    )
    try:
        await _consume(subscription, cfg, rollup, sink, feed, shutdown)
    finally:
        # Hand the subscription back, whatever ended the loop — a clean stop, a
        # cancellation, or a vertical's NotImplementedError. Skipping this is not
        # cosmetic: the connection's `drain()` in `main.py` waits on every live
        # subscription, so an orphaned one turns every shutdown into a 30-second
        # hang and then a SIGKILL from the orchestrator. The *durable consumer*
        # itself stays on the server (that is what remembers the offset); only
        # this client's interest in it goes away.
        with contextlib.suppress(Exception):
            await subscription.unsubscribe()
        log.info("consumer pipeline stopped")


async def _consume(
    subscription: JetStreamContext.PullSubscription,
    cfg: PipelineConfig,
    rollup: Rollup,
    sink: Sink,
    feed: LiveFeed,
    shutdown: asyncio.Event,
) -> None:
    """The loop itself, split out so `run` can own the subscription's lifetime."""
    while not shutdown.is_set():
        try:
            messages = await subscription.fetch(cfg.fetch_batch, timeout=cfg.flush_interval)
        except NatsTimeoutError:
            # No traffic this tick. Not an error — fall through to the flush so
            # an idle pipeline still closes its windows on schedule.
            messages = []
        except Exception as exc:  # noqa: BLE001 - a broker blip must not kill the loop
            log.error("consumer fetch failed", error=str(exc))
            await asyncio.sleep(cfg.flush_interval)
            continue

        for msg in messages:
            await process_message(rollup, msg)

        await flush_windows(rollup, sink, feed)
        try:
            await sink.flush()
        except Exception as exc:  # noqa: BLE001 - retried via redelivery (V3)
            log.error("time-triggered sink flush failed", error=str(exc))

    # Graceful shutdown: drain partial windows, do a final flush, exit.
    log.info("pipeline draining on shutdown")
    # TODO(V2 / graceful shutdown): use `rollup.drain_all()` here instead, so a
    # clean stop flushes the windows whose watermark hasn't passed yet rather
    # than dropping them. A crash may lose them (at-least-once covers it); a
    # deliberate stop should not.
    await flush_windows(rollup, sink, feed)
    try:
        await sink.flush()
    except Exception as exc:  # noqa: BLE001
        log.error("final sink flush failed", error=str(exc))


async def process_message(rollup: Rollup, msg: Msg) -> None:
    """Decode one message into points (V1) and fold them into the engine (V2).

    NOTE the ack ordering: acking here — before the rollups these points feed
    have been written — turns a crash into silent data loss. Getting it right is
    the V3 lesson.
    """
    try:
        points = parse.parse(msg.data.decode("utf-8", errors="strict"))
    except NotImplementedError:
        raise
    except Exception as exc:  # noqa: BLE001 - a poison message must not stop the stream
        log.warning("dropping unparseable message from stream", error=str(exc))
        await msg.ack()
        return

    for point in points:
        rollup.ingest(point)

    # TODO(V3): ack only AFTER the rollups these points feed have been flushed
    # and durably written. Move the ack into the flush path: hold the messages
    # whose points are still in open windows, and ack them once the batch
    # containing those windows lands in ClickHouse. Until then, redelivery is
    # your only safety net and this ack cancels it.
    await msg.ack()


async def flush_windows(rollup: Rollup, sink: Sink, feed: LiveFeed) -> None:
    """Flush closed windows (V2) into the batched sink (V3) and the feed (V4)."""
    rows = rollup.flush_ready(datetime.now(UTC))
    if not rows:
        return
    log.debug("flushing closed windows", count=len(rows))

    for row in rows:
        feed.publish(row)  # V4: live fan-out, drop-tolerant

    try:
        await sink.push(rows)
    except Exception as exc:  # noqa: BLE001
        # V3: must NOT drop on the durable path — log and let redelivery retry.
        log.error("sink push failed; rollups will be retried via redelivery", error=str(exc))
