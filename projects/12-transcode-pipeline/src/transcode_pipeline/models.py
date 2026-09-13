"""The domain types: jobs, the output ladder, and the nodes of the task DAG.

A **job** transcodes one source asset into a *ladder* of renditions. The work is
expressed as a **DAG of tasks** (V2): a `Split` fans out into one `Transcode` per
(chunk × rendition), and each rendition's `Stitch` fans those back in. The graph,
its edges, and each task's status + lease live in Postgres (`store.py`), so a
crashed worker's task becomes claimable again and the pipeline survives a restart.

Everything here is concrete — these are shapes, not the vertical. Two choices
worth knowing about:

* **A task's kind is a discriminated union, not a class hierarchy with a `run()`
  method.** The `op` field is the discriminator, and each model is exactly the
  JSONB the migration stores (`{"op": "transcode", "chunk": 3, "rendition":
  "720p"}`), so a row decodes with one `TASK_KIND.validate_python(...)` call and a
  worker dispatches with `match`. Behaviour lives in the worker; the kind is data
  that crosses the database.
* **Frozen models.** A task read out of the store is a snapshot of a row, not a
  live handle onto it. Letting code set `task.status` in memory invites the exact
  bug V2 is about — trusting the in-memory view over the durable one. A state
  change is a store call.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "TASK_KIND",
    "JobAccepted",
    "JobContext",
    "JobStatus",
    "JobView",
    "NewJob",
    "Rendition",
    "Split",
    "Stitch",
    "Task",
    "TaskCounts",
    "TaskKind",
    "TaskStatus",
    "Transcode",
]


class Rendition(BaseModel):
    """One rung of the output ABR ladder: a named quality target.

    V3 turns this into deterministic encoder flags, and the ladder is what makes
    the fan-out wide (chunks × renditions). Frozen, because one ladder is shared by
    every task of a job.

    TODO(security): nothing here is bounded yet. A client can send a 100-rung
    ladder at 8K and 900 Mbps — or a rung *named* `../../etc`, and `name` becomes a
    directory under `WORK_DIR` (see `WorkDir.chunk_dir`). Validating and bounding
    the ladder is a Security checklist item.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    """Ladder rung name, also the output subdirectory (e.g. `720p`)."""
    height: int
    """Target height in pixels; width follows from the source aspect ratio."""
    v_bitrate_kbps: int
    """Target video bitrate."""
    a_bitrate_kbps: int
    """Target audio bitrate."""


class TaskStatus(StrEnum):
    """Where a task sits in its lifecycle.

    The values are the `task_status` enum in `migrations/0001_init.sql`, spelled
    identically, so a row's status parses with `TaskStatus(row["status"])` and a
    parameter binds as the plain string. A test holds the two in step.
    """

    PENDING = "pending"
    """Created but not yet runnable — waiting on upstream dependencies."""
    READY = "ready"
    """Every dependency is `DONE`; claimable by a worker."""
    RUNNING = "running"
    """Claimed and leased to a worker right now."""
    DONE = "done"
    """Finished successfully; its artifact exists."""
    FAILED = "failed"
    """Exhausted its retries — dead-lettered. A job with any failed task is failed."""


class JobStatus(StrEnum):
    """A job's lifecycle — the `job_status` enum in the migration."""

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Split(BaseModel):
    """Probe the source, plan keyframe-aligned chunks (V1), and expand the DAG with
    the per-chunk transcodes and per-rendition stitches (V2)."""

    model_config = ConfigDict(frozen=True)

    op: Literal["split"] = "split"


class Transcode(BaseModel):
    """Transcode exactly one chunk at one rendition (V3).

    Idempotent: re-running it must reproduce the same chunk bytes.
    """

    model_config = ConfigDict(frozen=True)

    op: Literal["transcode"] = "transcode"
    chunk: int
    """The chunk's index in V1's plan — also its artifact's file name."""
    rendition: str
    """Which ladder rung, by name."""


class Stitch(BaseModel):
    """Concatenate + remux one rendition's chunks into the final output (V4).

    Depends on *every* `Transcode` of that rendition — the fan-in.
    """

    model_config = ConfigDict(frozen=True)

    op: Literal["stitch"] = "stitch"
    rendition: str


TaskKind = Annotated[Split | Transcode | Stitch, Field(discriminator="op")]
"""What a task does. The dependency shape follows from the kind:
`Split` → many `Transcode` → one `Stitch` per rendition."""

TASK_KIND: Final = TypeAdapter[TaskKind](TaskKind)
"""Decode a `kind` JSONB value (a `dict`) into its model, or dump one back.

An unknown `op` is a `ValidationError` naming the discriminator, never a task a
worker silently doesn't know how to run."""


class Task(BaseModel):
    """A node in the job DAG — a snapshot of one `tasks` row plus its edges."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    job_id: UUID
    kind: TaskKind
    status: TaskStatus
    deps: tuple[UUID, ...] = ()
    """Upstream tasks that must be `DONE` before this one becomes `READY`."""
    attempts: int = 0
    """How many times this task has been claimed (retry/backoff, V3)."""
    lease_until: datetime | None = None
    """When the current lease expires; past it the reaper reclaims the task (V3).
    `None` unless the task is `RUNNING`."""


class NewJob(BaseModel):
    """The body of `POST /jobs`."""

    source: str
    """Path to the source file, relative to `WORK_DIR`. Untrusted input — see
    `WorkDir.resolve_source` before anything opens it."""
    ladder: list[Rendition] = Field(default_factory=list[Rendition])
    """Output ladder; empty or absent means "use the server default ladder"."""


class JobAccepted(BaseModel):
    """The `202 Accepted` body: the id to poll."""

    id: UUID


class TaskCounts(BaseModel):
    """Per-status task counts. Every field defaults to zero, because a status no
    task is in is simply absent from a `GROUP BY status`."""

    pending: int = 0
    ready: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0


class JobView(BaseModel):
    """What `GET /jobs/{id}` returns — the JSON a dashboard polls.

    This model *is* the polling contract: FastAPI publishes it as a schema at
    `/docs`, and renaming a field here breaks every consumer silently.
    """

    id: UUID
    source: str
    ladder: list[Rendition]
    status: JobStatus
    created_at: datetime
    tasks: TaskCounts
    """Per-status task counts, so a caller can watch the DAG drain
    (e.g. `{"done": 40, "running": 4, "ready": 12, "pending": 8, "failed": 0}`)."""


class JobContext(BaseModel):
    """What a worker needs to run a job's `Split`: the source and the ladder."""

    model_config = ConfigDict(frozen=True)

    source: str
    """Still the untrusted, client-supplied path — resolve it before use."""
    ladder: tuple[Rendition, ...]
