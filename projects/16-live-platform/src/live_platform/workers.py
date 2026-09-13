"""V2 — Autoscaling transcode worker pool. `src/live_platform/workers.py`.

Transcoding an ABR ladder is the CPU-heavy part of the pipeline, and load is
*bursty*: one popular streamer going live can 10× the work in seconds. This is
the vertical where you do **real k8s ops** — the worker Deployment scales on
demand, and the two things that make that safe are yours to build:

1. **The autoscaler signal.** HPA cannot see "transcode backlog" on its own. You
   expose backlog as a metric a custom/external-metrics HPA reads, so replicas
   track backlog instead of CPU alone — `desired_replicas` is the shape of it.
2. **At-least-once leasing under pod churn.** When HPA scales *down* (or a node
   preempts a pod), an in-flight job must not be lost or done twice. A worker
   *claims* a job under a visibility-timeout lease; if its pod dies mid-transcode
   the lease lapses and another worker retries — but a `complete` inside the
   lease acks it once. The same claim/lease idea as the job queue (project 04),
   now backing a real Deployment with a PodDisruptionBudget and a graceful drain.

## What the broker gives you, and what it does not

NATS JetStream already has the lease: a pull consumer's `ack_wait` is a
visibility timeout, and a message not acked inside it is redelivered. So the
lease is not a timer you write — it is a number you *choose*, and the choice
cuts both ways. Too short, and a slow-but-alive worker has its job redelivered
to a second worker mid-flight. Too long, and a pod the HPA just killed holds a
job invisible for that whole window, while the backlog you autoscale on quietly
lies to you. A worker that legitimately needs longer can extend its own lease
as it works rather than you raising the ceiling for everyone.

What the broker does *not* give you is "exactly once". Redelivery is how
at-least-once survives a dead worker, so the same job *will* sometimes run
twice. "A completed job is acked exactly once" is about making the second run
harmless, not about preventing it.

## Why CPU is the wrong signal

A worker blocked on I/O reads idle while its backlog grows, and a fleet scaled on
CPU reacts after viewers have already seen the stall. Backlog leads; CPU lags.

Scaffold state: the pool is constructed and its best-effort depth reads. The
queue, the lease and the signal are the V2 worklist.
"""

from __future__ import annotations

from nats.js import JetStreamContext
from pydantic import BaseModel, ConfigDict

__all__ = ["TranscodeJob", "WorkerPool"]


class TranscodeJob(BaseModel):
    """One unit of transcode work: turn `stream_key`'s source into one ABR rung.

    A stream fans out into one job per ladder rung. A pydantic model so the wire
    encoding is free in both directions — `job.model_dump_json()` onto the queue,
    `TranscodeJob.model_validate_json(data)` off it — and a malformed message is
    a `ValidationError` naming the field rather than a `KeyError` three calls in.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    stream_key: str
    rendition: str
    """Which ladder rung this job produces, e.g. `"720p"`."""
    enqueued_at_ms: int
    """Unix millis the job was enqueued (queue-wait latency and lease math)."""


class WorkerPool:
    """The transcode pool's control surface.

    Workers themselves are separate k8s pods. This is what *enqueues* jobs (from
    the control plane on `→ TRANSCODING`), exposes the autoscaler signal, and —
    inside a worker — claims and acks jobs.
    """

    def __init__(
        self,
        js: JetStreamContext,
        *,
        stream_name: str,
        lease_secs: float,
        target_backlog_per_worker: int,
        max_replicas: int,
    ) -> None:
        self._js = js
        self.stream_name = stream_name
        """JetStream stream backing the queue."""
        self.lease_secs = lease_secs
        """How long a claimed job stays invisible before it is presumed abandoned."""
        self.target_backlog_per_worker = target_backlog_per_worker
        """Backlog per replica the autoscaler aims for — how hard replicas track depth."""
        self.max_replicas = max_replicas
        """Replica ceiling the pool will ask the HPA for."""
        self._queue_depth = 0

    @property
    def queue_depth(self) -> int:
        """Best-effort backlog: jobs waiting, not yet claimed.

        A mirror, not the truth. The JetStream consumer's pending count is the
        source of truth; this exists so `/status` and the gauge read backlog
        without a broker round-trip per scrape. Note it starts at zero on boot
        while the stream may already hold thousands — keeping it honest across a
        restart is part of V2.
        """
        return self._queue_depth

    # ---- V2 worklist: the queue, the lease, and the autoscale signal -----------

    async def ensure_queue(self) -> None:
        """Create-or-get the JetStream stream and its durable pull consumer.

        TODO(V2): idempotent — every pod runs this at startup, concurrently,
        during a rolling deploy. The consumer's `ack_wait` *is* the lease; see
        the module docstring on choosing it.
        """
        raise NotImplementedError("V2: create-or-get the JetStream stream + pull consumer")

    async def enqueue(self, job: TranscodeJob) -> None:
        """Publish one job onto the durable queue.

        TODO(V2): called by the control plane, once per ladder rung, on
        `→ TRANSCODING`. Nothing on the ingest path may block waiting for a
        *worker* — only for the broker's publish ack, which is what makes the job
        durable before this returns.
        """
        raise NotImplementedError("V2: publish the job to JetStream, bump the depth")

    async def claim(self, worker_id: str) -> TranscodeJob | None:
        """Claim the next job under a lease, or `None` when there is none.

        TODO(V2): no other worker gets the same job while the lease holds. If
        this worker's pod dies before `complete`, the lease lapses and the job is
        redelivered — at-least-once, never lost. An empty queue is a normal
        answer, not an exception: a pull that times out empty comes out of here
        as `None`.
        """
        raise NotImplementedError("V2: pull one message under the lease, drop the depth")

    async def complete(self, job_id: str) -> None:
        """Ack a finished job inside its lease so it is removed.

        TODO(V2): `complete` takes an id, but an ack needs the message the job
        arrived on — so something has to remember which leased message belongs to
        which job between `claim` and here. A second `complete` for the same id
        (a redelivery raced a slow ack) must be harmless.
        """
        raise NotImplementedError("V2: ack the message so it is not redelivered")

    def desired_replicas(self) -> int:
        """The replica count the HPA should scale the worker Deployment to.

        TODO(V2): `ceil(queue_depth / target_backlog_per_worker)`, clamped to
        `[1, max_replicas]`. Monotonic in backlog, and never zero — a Deployment
        scaled to zero has nobody left to notice the next job. Note that Python's
        `//` floors, which is not the rounding this formula asks for.
        """
        raise NotImplementedError("V2: derive replicas from depth, clamp to [1, max_replicas]")
