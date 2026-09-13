"""V1 — Stream control plane / session lifecycle. `src/live_platform/control.py`.

This is the brain of the platform: the thing that knows *"stream `abc123` is
live, ingested on node-2, transcoded to a 3-rung ABR ladder, HLS playing at
`/live/abc123/…`"*. When an RTMP/WebRTC ingest connects, a session is born and
walks a state machine — `OFFLINE → INGESTING → TRANSCODING → LIVE → ENDED` — and
each transition fans work out to the other planes: enqueue transcode jobs (V2),
point the edge at the packager (V3), open the chat channel (V4). When ingest
drops, it tears all of that down.

The learning here is **orchestration under failure**: transitions must be
*idempotent* (the same ingest webhook can fire twice), the durable record
(Postgres) is the source of truth so a control-plane restart *reconciles* live
streams rather than losing them, and a half-finished stream never strands
transcode workers or leaks an edge route.

## The Python trap in "just a dict"

The Rust kept the registry behind an `RwLock`. Here it is a plain `dict`, and
that is correct — one event loop, one thread, so no lock is needed to read or
write it safely. But do not mistake "no data race" for "no race". Every `await`
is a point where another request runs. An ingest-start that checks *"is this key
already live?"*, then awaits a Postgres insert, then writes the registry, has a
window the width of a network round-trip in which a duplicate webhook runs the
same check and gets the same answer.

The lock you do not need for memory safety, you may still need for
*atomicity* — or you can push the check-and-act down into a single SQL
statement, where a constraint makes it atomic for you. The migration already
carries a partial unique index; read `migrations/0001_init.sql` before deciding.

Scaffold state: the constructor and the read-only `snapshot` / `live_count` are
wired, so the server boots and `/status` shows an (empty) registry. The
state-machine methods are the V1 worklist.
"""

from __future__ import annotations

from enum import StrEnum

import asyncpg
from pydantic import BaseModel, ConfigDict

from .config import Rendition

__all__ = ["ControlPlane", "StreamSession", "StreamState"]


class StreamState(StrEnum):
    """The lifecycle of one live stream.

    A `StrEnum` so the value in the `stream_sessions.state` column, the JSON on
    `/status`, and the `to` label on the transitions counter are one lowercase
    string, with no conversion at any of those three boundaries.

    Which edges between these are *legal* — and what each sets in motion — is
    the heart of V1, and is deliberately not encoded here.
    """

    OFFLINE = "offline"
    """Known stream key, nobody broadcasting."""
    INGESTING = "ingesting"
    """Ingest connected; bytes arriving but no renditions yet."""
    TRANSCODING = "transcoding"
    """Transcode jobs enqueued; the ABR ladder filling in."""
    LIVE = "live"
    """At least the source rendition is packaged and playable at the edge."""
    ENDED = "ended"
    """Ingest dropped; the session is draining or archived."""


class StreamSession(BaseModel):
    """The durable record for one stream's current session.

    Persisted in Postgres (the source of truth) and mirrored in the registry for
    hot `/status` and routing reads. Frozen, so a transition produces a *new*
    session (`session.model_copy(update=...)`) rather than mutating one another
    coroutine is halfway through serialising.

    `stream_key` is both the broadcaster's ingest secret and the playback URL
    slug — that is how the schema was designed, and it means this record is not
    safe to show a viewer as-is. The Security checklist asks you to keep the key
    out of logs and error bodies; decide what `/status` should show while you
    are there.
    """

    model_config = ConfigDict(frozen=True)

    stream_key: str
    state: StreamState
    ingest_node: str | None = None
    """Which ingest node holds the RTMP/WebRTC connection, if any."""
    ladder: tuple[Rendition, ...] = ()
    """The ABR ladder this session is transcoded into."""
    started_at_ms: int | None = None
    """Unix millis the current live session started (uptime and archival)."""


class ControlPlane:
    """A durable session store plus a hot in-memory registry."""

    def __init__(
        self,
        pool: asyncpg.Pool[asyncpg.Record],
        *,
        ladder: tuple[Rendition, ...],
        max_streams: int,
        segment_secs: float,
        part_secs: float,
    ) -> None:
        self._pool = pool
        self.ladder = ladder
        self.max_streams = max_streams
        """Past this, a new ingest is rejected rather than degrading every live stream."""
        self.segment_secs = segment_secs
        self.part_secs = part_secs
        self._registry: dict[str, StreamSession] = {}
        """Hot cache of sessions by stream key. Postgres is the truth; this is
        rebuilt from it on startup (`reconcile`) and kept in step per transition."""

    def snapshot(self) -> list[StreamSession]:
        """Every session in the registry, for `/status`.

        A new list, so a caller iterating it cannot be tripped by a transition
        that lands mid-iteration. The sessions are frozen, so copying the list
        is enough.
        """
        return list(self._registry.values())

    @property
    def live_count(self) -> int:
        """Streams currently `LIVE` — the `live_streams` gauge reads this."""
        return sum(1 for s in self._registry.values() if s.state is StreamState.LIVE)

    # ---- V1 worklist: the state machine + reconciliation -----------------------

    async def reconcile(self) -> None:
        """Rebuild the registry from Postgres on startup.

        TODO(V1): a control-plane restart must *recover* in-progress streams
        (ingesting / transcoding / live) instead of losing them, and a session
        whose ingest lease has expired must be reconciled to `ENDED` — a ghost
        "live" stream nobody is broadcasting is the failure this prevents.
        Called once from `main` before serving, when `RUN_BACKGROUND=true`.

        Think desired vs. actual: Postgres says what *should* be live, the lease
        says whether it still *is*. The registry is only ever a copy of the answer.
        """
        raise NotImplementedError("V1: load sessions from Postgres, end expired leases")

    async def on_ingest_start(self, stream_key: str, ingest_node: str) -> StreamSession:
        """An ingest node reports a broadcaster connected.

        TODO(V1): reject an unknown key *before* allocating anything, enforce
        `max_streams`, then create (or return the existing) session and move it
        `OFFLINE → INGESTING`. **Idempotent**: the same webhook delivered twice
        returns the same session — not a second one, not a second transcode
        enqueue, not a double-counted metric. See the module docstring on why
        "check, await, then act" is not idempotent on its own.

        Raises `RejectedError` for an unknown key, `ConflictError` past the cap.
        """
        raise NotImplementedError("V1: admit the stream, persist INGESTING, seed the registry")

    async def transition(self, stream_key: str, to: StreamState) -> StreamSession:
        """Drive one legal state transition and fan out its side effects.

        TODO(V1): refuse an illegal edge (e.g. `OFFLINE → LIVE` with no ingest)
        with `ConflictError`, persist the new state, update the registry, and fire
        what the edge implies — one transcode job per rung on `→ TRANSCODING`
        (V2), the edge route on `→ LIVE` (V3).

        Worth deciding up front: does a side effect run before or after the state
        commits, and what does a retry see if it fails halfway? Those two answers
        are most of the design doc's V1 section.
        """
        raise NotImplementedError("V1: validate the edge, persist, update registry, fan out")

    async def on_ingest_stop(self, stream_key: str) -> None:
        """Ingest dropped: end the session and release everything it held.

        TODO(V1): move the session to `ENDED`, tear down its transcode work, edge
        route and chat channel, and finalise the row. Must be safe to call for a
        stream that already ended or never existed — a stop webhook retried after
        a timeout is normal traffic, not an error.
        """
        raise NotImplementedError("V1: finalise the session, release resources, mark ENDED")
