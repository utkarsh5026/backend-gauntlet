"""V6 — Event source mapping: the poller that turns a stream into invocations.

For a stream, nothing calls Lambda. Lambda calls **you**: a managed poller reads a
shard, accumulates a batch, invokes the function once with the whole batch, and
only then checkpoints. The source here is project **23**'s stream — the same shard
iterators, the same per-partition ordering.

Three subtleties, and all three are where the data loss lives:

  * **Checkpoint after, never before.** Advance the iterator before the batch is
    processed and a crash silently skips records. Advance it after and a crash
    replays them — which is why the delivery guarantee is at-least-once and why the
    handler has to be idempotent.
  * **The poison pill.** One record the handler can never process, with a naive
    retry policy, blocks its shard *forever*. Everything behind it stops. The shard
    goes quiet and iterator age climbs, and it looks like the producer stopped.
  * **Partial batch failure.** A batch of 100 where record 60 fails: re-delivering
    all 100 re-runs 59 successes. Reporting item-level failures is the fix, and
    the ordering rule (everything after the failure retries too) is what keeps
    per-key ordering intact while doing it.

Scaffold state: the mapping, the batching policy and the poller loop are modelled;
polling, batching, dispatch and checkpointing raise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from .config import Settings
from .models import FunctionConfig

__all__ = ["EventSourceMapping", "MappingState", "Shard", "StreamRecord", "StartingPosition"]

log = structlog.get_logger(__name__)


class StartingPosition(StrEnum):
    """Where a mapping begins when it has no checkpoint."""

    TRIM_HORIZON = "TRIM_HORIZON"  # the oldest retained record — replay everything
    LATEST = "LATEST"  # only what arrives from now on


class MappingState(StrEnum):
    ENABLED = "Enabled"
    DISABLED = "Disabled"
    # A mapping can be disabled and re-enabled and must resume from its
    # checkpoint, not from the horizon — V6 grades on it.
    DISABLING = "Disabling"


@dataclass(slots=True)
class StreamRecord:
    """One change record as project 23's stream emits it."""

    sequence_number: str
    partition_key: str
    data: bytes
    # Wall clock from the producer — the input to iterator age, which is the metric
    # that tells you a consumer is falling behind BEFORE it becomes data loss.
    approximate_arrival_time: float

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.approximate_arrival_time)


@dataclass(slots=True)
class Shard:
    """One ordered partition of the stream, and where we are in it.

    `checkpoint` is the whole safety story: it is the sequence number we have
    *durably finished*, not the one we have read.
    """

    shard_id: str
    iterator: str | None = None
    checkpoint: str | None = None
    in_flight: int = 0
    # Consecutive failures on the same batch — the poison-pill counter.
    failure_streak: int = 0


@dataclass(slots=True)
class EventSourceMapping:
    """A poller binding one stream to one function."""

    uuid: str
    function: FunctionConfig
    source_url: str
    batch_size: int
    batch_window_seconds: float
    starting_position: StartingPosition = StartingPosition.TRIM_HORIZON
    state: MappingState = MappingState.ENABLED
    parallelisation_factor: int = 1
    shards: dict[str, Shard] = field(default_factory=dict[str, Shard])


class EventSourcePoller:
    """Runs one mapping: poll, batch, invoke, checkpoint.

    One task per shard. The per-shard task is what makes ordering possible at all:
    ordering is a property of a shard, so a design that fans records from many
    shards into one worker pool has already given it away.
    """

    def __init__(self, settings: Settings, mapping: EventSourceMapping) -> None:
        self._settings = settings
        self.mapping = mapping
        # TODO(V6): the HTTP client for the source and the per-shard task handles.
        # Reuse ONE `httpx.AsyncClient` for the poller's lifetime — a client per
        # poll rebuilds the connection pool every time and shows up directly in
        # iterator age.

    async def run(self) -> None:
        """Poll the source until cancelled. Started by the lifespan in `main`."""
        # TODO(V6): discover shards, start one task per shard, and supervise them.
        # Supervision is not optional: a shard task that dies silently is a shard
        # that stops forever, and the only symptom is iterator age climbing on a
        # graph nobody is watching.
        raise NotImplementedError("V6: discover shards and run a poller task per shard")

    async def _poll_shard(self, shard: Shard) -> None:
        """One shard's loop: read, batch, dispatch, checkpoint."""
        # TODO(V6): the loop. Order matters:
        #
        #   1. read records from `shard.iterator` (project 23's
        #      `GET /streams/{table}?iterator=...`);
        #   2. accumulate until `batch_size` OR `batch_window_seconds` elapses —
        #      whichever comes FIRST. The window is what stops a low-rate stream
        #      from stalling until a full batch accumulates, and forgetting it is
        #      the classic way a quiet stream looks broken;
        #   3. invoke the function once with the whole batch;
        #   4. only then checkpoint.
        #
        # A trimmed iterator (project 23 fails these distinctly) must be handled
        # explicitly: re-open from TRIM_HORIZON and record that records were lost,
        # rather than silently skipping to LATEST and pretending nothing happened.
        raise NotImplementedError("V6: read, batch by size-or-window, invoke, then checkpoint")

    async def _dispatch(self, shard: Shard, batch: list[StreamRecord]) -> None:
        """Invoke the function with a batch and act on what comes back."""
        # TODO(V6): invoke, then handle the response's partial-batch report.
        #
        # The real contract: the handler returns
        # `{"batchItemFailures": [{"itemIdentifier": "<sequence_number>"}]}`. The
        # rule is to checkpoint up to the EARLIEST reported failure and retry from
        # there — everything after it retries too, even if it succeeded. That
        # looks wasteful and is the only choice that preserves ordering; record
        # that reasoning in the design doc.
        #
        # An empty `batchItemFailures` means the whole batch succeeded. A handler
        # that returns nothing at all means the same — but a handler that RAISED
        # means the whole batch failed, and those two must not be conflated.
        raise NotImplementedError("V6: invoke with the batch and apply its partial-failure report")

    async def _checkpoint(self, shard: Shard, sequence_number: str) -> None:
        """Durably record progress. Only ever moves forward."""
        # TODO(V6): persist it. Two invariants the SPEC tests:
        #
        #   * a checkpoint NEVER moves backwards, even if a retry re-processes
        #     older records — an out-of-order write here silently re-delivers
        #     everything after it, forever;
        #   * it must survive a restart, or "resumes from its checkpoint rather
        #     than the horizon" is not true.
        raise NotImplementedError("V6: durably advance the shard's checkpoint")

    def _handle_poison_pill(self, shard: Shard, batch: list[StreamRecord]) -> None:
        """Stop one bad record from blocking its shard forever."""
        # TODO(V6): pick a policy and make it explicit. The real service offers
        # three, and any of them is a defensible answer here so long as it is
        # documented and tested:
        #
        #   * bisect the batch on failure until the bad record is isolated;
        #   * a max-retries ceiling per batch, then skip;
        #   * an on-failure destination the record is sent to before skipping.
        #
        # What is NOT acceptable is retrying forever, because the shard stops and
        # the only symptom is a metric.
        raise NotImplementedError("V6: apply the poison-pill policy so the shard makes progress")

    def iterator_age_seconds(self) -> float:
        """How far behind the newest record we are — the health metric for a poller."""
        raise NotImplementedError("V6: age of the oldest un-processed record")
