"""Typed settings for the workflow engine.

Every field maps to a variable in `.env.example`, and every one has a working
default, so a bare `make run` against the compose Postgres starts a usable
engine. The annotation is the parser: `port: int = 7233` gets you the env lookup,
the coercion, the default, and a startup error naming the offending variable.

Several of these are *graded* knobs, not magic numbers — the visibility timeout,
the scan interval and the sticky TTL each buy something and cost something, and
`docs/21-design.md` is where the SPEC asks you to say which trade you made.
"""

from __future__ import annotations

from common_config import BaseConfig
from pydantic import Field

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- serving ---
    port: int = 7233
    """gRPC frontend — where workers and starters connect.

    7233 is Temporal's own frontend port (a nod); nothing forces it, but it keeps
    this engine off the rate limiter's 50051 if you run both.
    """

    metrics_port: int = 9121
    """HTTP port for `/healthz` + `/metrics` (repo convention: 9100 -> 91NN)."""

    log_level: str = "info"

    max_concurrent_rpcs: int = Field(default=1000, gt=0)
    """Server-side ceiling on in-flight RPCs — backpressure instead of an OOM.

    Long-poll makes this subtler than it looks: every parked poller *is* an
    in-flight RPC, so this number has to comfortably exceed the number of workers
    you expect to be polling at once, or workers starve each other before any
    real work is refused. Size it together with `db_pool_max`.
    """

    max_payload_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    """Cap on a workflow/activity input or result.

    Payloads are opaque bytes the engine stores and hands on — never interprets,
    never executes. Unbounded, they are a way to fill the history table with one
    RPC, so the SPEC grades rejecting an oversize one *cleanly*.
    """

    # --- durable store ---
    database_url: str = "postgres://workflow:workflow@localhost:5421/workflow"
    """Postgres holds all three durable things: history, task queues, timers.

    Host port 5421 = 54NN with NN=21; keep it in lockstep with
    `docker-compose.yml` and `.env.example`.
    """

    db_pool_min: int = Field(default=2, ge=1)
    db_pool_max: int = Field(default=20, ge=1)
    """Bounded on purpose (the "bounded pool sized on purpose" checklist item).

    The ceiling is a property of Postgres, not of this process: every pooled
    connection is a backend process on the server, so `db_pool_max` × replicas
    must stay under its `max_connections`. The floor matters too — a dispatcher
    holding a transaction open while another coroutine waits for a connection is
    how a long-poll turns into a self-inflicted deadlock.
    """

    # --- dispatch / long-poll (V4) ---
    long_poll_timeout_ms: int = Field(default=5_000, gt=0)
    """How long a poll blocks before returning "no work".

    Long enough to amortize the round-trip, short enough that a worker notices
    shutdown.
    """

    task_visibility_timeout_ms: int = Field(default=30_000, gt=0)
    """How long a claimed-but-uncompleted task stays invisible.

    Past it the task is assumed lost (the worker crashed) and becomes claimable
    again. This is the knob that makes delivery at-least-once — and it trades
    crash-recovery latency against the risk of two workers running one slow task.
    """

    # --- durable timers (V3) ---
    run_timer_service: bool = False
    """Off by default, so the bare scaffold serves the API without the scan loop
    raising `NotImplementedError` on its first pass. Flip on once V3 is built."""

    timer_scan_interval_ms: int = Field(default=200, gt=0)
    """How often the timer service scans for due timers.

    Smaller = tighter firing latency, more scan queries. The honest contract is
    "a timer fires within interval + ε of its due time", and which ε you pick is
    a tradeoff you get to measure.
    """

    timer_scan_batch: int = Field(default=100, gt=0)
    """How many due timers one scan pass claims.

    A bound, not a target: it keeps a single pass's transaction short so a burst
    of due timers is drained over several passes instead of one long lock.
    """

    # --- sticky cache (V5) ---
    sticky_ttl_ms: int = Field(default=10_000, gt=0)
    """How long an execution stays pinned to the worker that last ran it.

    Past it, follow-up tasks fall back to the normal queue and a full replay.
    This is what ties the cache to worker liveness — the pin has to expire
    faster than an execution can afford to be stranded on a dead worker.
    """

    @property
    def long_poll_timeout(self) -> float:
        """The long-poll window in seconds — what asyncio APIs want."""
        return self.long_poll_timeout_ms / 1000

    @property
    def visibility_timeout(self) -> float:
        """The visibility timeout in seconds."""
        return self.task_visibility_timeout_ms / 1000

    @property
    def timer_scan_interval(self) -> float:
        """The timer scan interval in seconds."""
        return self.timer_scan_interval_ms / 1000

    @property
    def sticky_ttl(self) -> float:
        """The stickiness window in seconds."""
        return self.sticky_ttl_ms / 1000
