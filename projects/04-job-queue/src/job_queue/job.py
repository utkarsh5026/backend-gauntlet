"""Shared job types: the row shape, its lifecycle state, and the enqueue input.

These are the values the verticals pass around — `enqueue` takes a :class:`NewJob`,
`claim` hands a worker a :class:`Job`, and the worker drives it through
:class:`JobState`.

The validation story is where this differs most from a statically-typed port. In
Rust the caps lived in a hand-written `validate()` returning a reason string; here
the *model is the validator*. `Field(max_length=…)`, a charset `pattern`, and
`ge`/`le` bounds are declarations pydantic enforces on construction, so there is no
separate function that can drift from the type. `routes` turns the resulting
`ValidationError` into the 400 the SPEC asks for.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "MAX_ATTEMPTS_CEILING",
    "MAX_DELAY_SECS",
    "MAX_NAME_LEN",
    "MAX_PAYLOAD_BYTES",
    "Job",
    "JobId",
    "JobState",
    "NewJob",
]

JobId = int
"""Database identity of a job (`jobs.id`, a `BIGSERIAL`)."""

MAX_PAYLOAD_BYTES: Final = 64 * 1024
"""Max serialized bytes of a job `payload` (64 KiB).

The *semantic* cap on the payload specifically; the request-body limit in `routes`
is a coarser outer guard. A payload is a *reference to work* (an id / object key),
not the work itself — and the row it lands in is re-read on every claim, retry,
and GET."""

MAX_NAME_LEN: Final = 64
"""Max length, in bytes, of the `queue` / `kind` identifiers."""

MAX_ATTEMPTS_CEILING: Final = 25
"""Inclusive ceiling on a caller-supplied `max_attempts` — stops a caller turning a
poison job into a million-retry slow loop that never reaches the DLQ."""

MAX_DELAY_SECS: Final = 30 * 24 * 60 * 60
"""Ceiling on `delay_secs` (30 days) — bounds how far a job may be scheduled out."""

_NAME_PATTERN: Final = r"^[A-Za-z0-9_-]+$"
"""A `queue`/`kind` identifier is ASCII alphanumerics, `_`, or `-` only.

These names become a `NOTIFY` channel (`jobs_{queue}`) and a metric label, so
arbitrary or huge values would blow up label cardinality and produce surprising
channel names."""

Name = Annotated[str, Field(min_length=1, max_length=MAX_NAME_LEN, pattern=_NAME_PATTERN)]
"""A validated `queue`/`kind` identifier — the cap and the charset in the type."""


class JobState(StrEnum):
    """Where a job is in its lifecycle. Persisted as the `jobs.state` text column.

    A `StrEnum` because the column *is* text: members compare equal to their own
    string, so the value read back from Postgres needs no decoding step and the
    value written needs no encoding one.
    """

    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"


class Job(BaseModel):
    """One row of the `jobs` table, as the queue hands it to a worker."""

    id: JobId
    queue: str
    kind: str
    payload: Any
    state: JobState
    attempts: int
    max_attempts: int
    run_at: datetime
    locked_until: datetime | None
    last_error: str | None
    created_at: datetime


class NewJob(BaseModel):
    """The enqueue request body — everything the caller controls, already bounded.

    Every constraint here is a SPEC security criterion ("validate and **cap**
    everything the caller controls"). They are declared rather than checked so the
    cap and the type can never disagree.
    """

    queue: Name = "default"
    kind: Name
    payload: Any = Field(default_factory=dict)
    """The job's arguments. An omitted or `null` payload is normalized to `{}`.

    That normalization is not cosmetic — it is an asyncpg impedance point. asyncpg
    maps Python `None` to **SQL NULL** for every type, including `JSONB`; there is
    no way to make it emit the JSONB value `null` from `None`. The `payload` column
    is `NOT NULL DEFAULT '{}'`, so a `None` here would be rejected by the database
    rather than stored as "no arguments". Since a JSON `null` payload and an empty
    object carry exactly the same information — this job takes no arguments —
    collapsing them at the boundary is the honest fix."""
    max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_CEILING)
    """Optional override; falls back to the server default when `None`."""

    delay_secs: int | None = Field(default=None, le=MAX_DELAY_SECS)
    """Delay before the job becomes eligible, in seconds. `None`/`0` = run now (V4).

    Only the *upper* bound is enforced here; a negative delay is clamped to `now()`
    by `Queue.enqueue` rather than rejected, so a caller whose clock skewed
    backwards still gets a runnable job instead of a 400."""

    @model_validator(mode="after")
    def _normalize_null_payload(self) -> NewJob:
        """Collapse an explicit `null` payload to `{}` — see the field's note."""
        if self.payload is None:
            self.payload = {}
        return self

    @model_validator(mode="after")
    def _cap_payload_bytes(self) -> NewJob:
        """Reject a payload whose *serialized* size exceeds the cap.

        Sized after serialization because that is what actually lands in the JSONB
        column and gets re-read on every claim, retry, and GET — the in-memory
        object graph is not the thing being bounded.
        """
        size = len(json.dumps(self.payload, separators=(",", ":")).encode())
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload too large: {size} bytes (max {MAX_PAYLOAD_BYTES})")
        return self
