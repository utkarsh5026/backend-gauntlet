"""HTTP surface + the per-connection WebSocket loop.

The wiring here is complete: the router, the `GET /ws` upgrade, the writer task
that drains a connection's outbox to the socket, frame parsing, and teardown on
every exit path. What it *calls into* — the hub, the mailbox's overflow policy,
presence, the cluster bridge — is where the SPEC lives. Run this as-is with
`CLUSTER=true` and a publish hits V4's `NotImplementedError`, which is the
worklist.

## The two-task shape

One connection is **two** concurrent jobs: reading client frames and writing
broadcasts out. They cannot be one loop, because `await websocket.receive_text()`
would block the writer while nothing is arriving. So the reader stays on the
handler's own task and the writer gets `asyncio.create_task`.

The rule that comes with that split: **only the writer task ever sends.**
Starlette's WebSocket is not safe for concurrent sends — two coroutines calling
`send_text` can interleave frames on the wire. Everything the server wants to
say, including protocol errors, goes through the mailbox instead. That is also
why an error frame can be shed by the overflow policy like any other message,
which is the right call: a client too far behind to receive data is too far
behind to receive complaints about it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable
from typing import Annotated, cast

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Body, Depends, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text

from .backpressure import Mailbox
from .directory import Directory, Group, Membership, Person
from .errors import AppError, BadRequest, StoreError, Unavailable
from .presence import PresenceRegistry
from .protocol import (
    BroadcastMessage,
    ClientMessage,
    ConnId,
    ErrorMessage,
    Heartbeat,
    PresenceMessage,
    Publish,
    Subscribe,
    Unsubscribe,
    client_message_adapter,
    conn_label,
    next_conn_id,
)
from .state import AppState

__all__ = ["router"]

log = structlog.get_logger(__name__)

router = APIRouter()

# How long one dependency probe may run before we call it down — stops a dead
# store from hanging the endpoint (and the devtools poll behind it).
PROBE_TIMEOUT = 2.0

# Client display identities are capped so a client cannot wedge the presence map
# with an absurd key (SPEC: cap everything a client controls).
MAX_IDENTITY_LEN = 64


def get_state(request: Request) -> AppState:
    return cast(AppState, request.app.state.app_state)


State = Annotated[AppState, Depends(get_state)]


def _directory(state: AppState) -> Directory:
    """Pull the directory out of state, or 503 if the roster DB is disabled.

    The pub/sub core runs without it; only `/admin` needs it.
    """
    if state.directory is None:
        raise Unavailable("directory disabled: set DATABASE_URL")
    return state.directory


# --- liveness -----------------------------------------------------------------


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Bare liveness for scrapers — deliberately does not touch a dependency.

    A liveness probe that pings Postgres restarts your app when Postgres blips,
    which is exactly backwards. Dependency state lives at `/debug/health`.
    """
    return {"status": "ok"}


# --- dependency health (devtools) ---------------------------------------------
#
# `GET /debug/health` live-probes the OPTIONAL backing stores so the web
# devtools panel can show an up/down light for each. NOT the liveness probe and
# NOT part of the store-free pub/sub core — playground observability, same tier
# as the `/admin` roster.


class DepStatus(BaseModel):
    """Status of one backing store. `state` is `"up" | "down" | "disabled"`."""

    state: str
    detail: str | None = None
    """The error on `down`, the reason on `disabled`; omitted when `up`."""
    latency_ms: float | None = None
    """Probe round-trip in milliseconds — present only when `up`."""

    @classmethod
    def up(cls, elapsed: float) -> DepStatus:
        return cls(state="up", latency_ms=round(elapsed * 1000, 2))

    @classmethod
    def down(cls, detail: str) -> DepStatus:
        return cls(state="down", detail=detail)

    @classmethod
    def disabled(cls, reason: str) -> DepStatus:
        return cls(state="disabled", detail=reason)


class DepsHealth(BaseModel):
    """A snapshot of every backing store the app can talk to."""

    db: DepStatus
    """Postgres (admin roster). `disabled` when `DATABASE_URL` is unset."""
    redis: DepStatus
    """Redis (cross-node bus). Probed for reachability even in single-node mode."""
    cluster_mode: bool
    """Whether the app is actually bridging through Redis (V4 / `CLUSTER=true`).
    `false` means Redis may be up but the pub/sub core is not using it."""
    ws_auth_configured: bool
    """Whether `WS_AUTH_TOKEN` is set. When `false`, EVERY `/ws` upgrade is
    rejected (fail closed) and nobody can come online. Only the boolean is
    reported — never the secret itself."""


async def _probe_db(state: AppState) -> DepStatus:
    if state.directory is None:
        return DepStatus.disabled("DATABASE_URL unset")
    started = time.perf_counter()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with state.directory.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return DepStatus.up(time.perf_counter() - started)
    except TimeoutError:
        return DepStatus.down("probe timed out")
    except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
        return DepStatus.down(str(exc) or type(exc).__name__)


async def _probe_redis(state: AppState) -> DepStatus:
    started = time.perf_counter()
    client: aioredis.Redis | None = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            # A fresh client, independent of the cluster bridge, so the panel
            # reports reachability even when we are single-node and never
            # actually bridge through it.
            client = aioredis.from_url(  # pyright: ignore[reportUnknownMemberType]
                state.settings.redis_url
            )
            # redis-py types PING's signature as `**kwargs: Any`, so strict
            # mode cannot see through it. The awaited result is a bool we
            # deliberately ignore — reaching this line at all is the probe.
            await client.ping()  # pyright: ignore[reportUnknownMemberType]
        return DepStatus.up(time.perf_counter() - started)
    except TimeoutError:
        return DepStatus.down("probe timed out")
    except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
        return DepStatus.down(str(exc) or type(exc).__name__)
    finally:
        if client is not None:
            await client.aclose()


@router.get("/debug/health")
async def deps_health(state: State) -> DepsHealth:
    # Both probes at once: they are independent and each may sit on the timeout,
    # so running them in sequence would double the worst case for no reason.
    db, redis_status = await asyncio.gather(_probe_db(state), _probe_redis(state))
    return DepsHealth(
        db=db,
        redis=redis_status,
        cluster_mode=state.cluster is not None,
        ws_auth_configured=bool(state.settings.ws_auth_token.get_secret_value()),
    )


# --- admin directory handlers -------------------------------------------------


class NewPerson(BaseModel):
    """`POST /admin/people` body. The avatar is a Notion-style emoji on a
    background color; both default if the client omits them."""

    name: str
    emoji: str = "🧘"
    color: str = "#6366f1"


class NewGroup(BaseModel):
    """`POST /admin/groups` body. A group's `name` is the topic sockets
    subscribe to; `emoji` + `color` are its avatar."""

    name: str
    emoji: str = "🎨"
    color: str = "#6366f1"


async def _store[T](operation: Awaitable[T]) -> T:
    """Run a directory query, turning a driver failure into a 500.

    `AppError` is re-raised untouched: `_directory` raises `Unavailable` (503)
    when the roster DB is switched off, and blanket-wrapping that into a
    `StoreError` would report a deliberate configuration as a server fault.
    """
    try:
        return await operation
    except AppError:
        raise
    except Exception as exc:
        raise StoreError(str(exc)) from exc


@router.get("/admin/people")
async def list_people(state: State) -> list[Person]:
    return await _store(_directory(state).list_people())


@router.post("/admin/people")
async def create_person(state: State, body: Annotated[NewPerson, Body()]) -> Person:
    name = body.name.strip()
    if not name:
        raise BadRequest("name must not be empty")
    return await _store(_directory(state).create_person(name, body.emoji, body.color))


@router.delete("/admin/people/{person_id}", status_code=204)
async def delete_person(state: State, person_id: uuid.UUID) -> Response:
    await _store(_directory(state).delete_person(person_id))
    return Response(status_code=204)


@router.get("/admin/groups")
async def list_groups(state: State) -> list[Group]:
    return await _store(_directory(state).list_groups())


@router.post("/admin/groups")
async def create_group(state: State, body: Annotated[NewGroup, Body()]) -> Group:
    name = body.name.strip()
    if not name:
        raise BadRequest("group name must not be empty")
    return await _store(_directory(state).create_group(name, body.emoji, body.color))


@router.post("/admin/people/{person_id}/groups/{group_id}", status_code=204)
async def add_member(state: State, person_id: uuid.UUID, group_id: uuid.UUID) -> Response:
    await _store(_directory(state).add_member(person_id, group_id))
    return Response(status_code=204)


@router.delete("/admin/people/{person_id}/groups/{group_id}", status_code=204)
async def remove_member(state: State, person_id: uuid.UUID, group_id: uuid.UUID) -> Response:
    await _store(_directory(state).remove_member(person_id, group_id))
    return Response(status_code=204)


@router.get("/admin/memberships")
async def list_memberships(state: State) -> list[Membership]:
    return await _store(_directory(state).memberships())


# --- the WebSocket ------------------------------------------------------------


class WsQuery(BaseModel):
    """Query params accepted on `GET /ws`.

    A browser `WebSocket` cannot set custom headers on the handshake, so the
    shared secret rides the query string instead — the one channel a browser
    client actually controls pre-upgrade.
    """

    token: str = ""
    identity: str | None = Field(default=None, max_length=512)
    """Display identity the client claims (e.g. the person's name from the admin
    panel). **Never trusted for anything but display** (SPEC security) —
    sanitized and capped below, and it only ever feeds the presence roster."""


def resolve_identity(claimed: str | None, conn: ConnId) -> str:
    """Sanitize the client-claimed identity: trim, cap, fall back to the
    connection id when missing or blank. Display-only — never an authz input."""
    if claimed is None:
        return conn_label(conn)
    trimmed = claimed.strip()[:MAX_IDENTITY_LEN]
    return trimmed or conn_label(conn)


def presence_snapshot(presence: PresenceRegistry, topic: str) -> PresenceMessage:
    """Snapshot `topic`'s current roster as a `presence` frame.

    A full roster, not a "someone joined/left" delta: presence frames ride the
    same bounded mailbox as everything else (V2), so they can be dropped under
    backpressure. A snapshot self-heals on the next join/leave; a delta would
    leave a client permanently wrong about who is in the room.
    """
    return PresenceMessage(topic=topic, members=presence.identities(topic))


async def _writer(websocket: WebSocket, mailbox: Mailbox) -> None:
    """Drain this connection's outbox to the socket until it closes.

    Backpressure is upstream (the bounded mailbox); this just forwards. Note
    that `send_text` awaits — that is the point, and it is *why* the mailbox has
    to be bounded: this task moves at the client's TCP speed, and everything
    that piles up while it waits lands in the queue.
    """
    while True:
        message = await mailbox.recv()
        if message is None:
            return
        try:
            await websocket.send_text(message.model_dump_json())
        except (WebSocketDisconnect, RuntimeError):
            # RuntimeError is what Starlette raises for a send after close.
            return


async def dispatch(
    state: AppState,
    conn: ConnId,
    identity: str,
    mailbox: Mailbox,
    command: ClientMessage,
) -> None:
    """Apply one decoded client command."""
    match command:
        case Subscribe(topic=topic):
            state.hub.subscribe(topic, conn, mailbox)
            state.presence.join(topic, conn, identity)
            state.hub.publish(topic, presence_snapshot(state.presence, topic))

        case Unsubscribe(topic=topic):
            state.hub.unsubscribe(topic, conn)
            state.presence.leave(topic, conn)
            state.hub.publish(topic, presence_snapshot(state.presence, topic))

        case Publish(topic=topic, payload=payload):
            state.hub.publish(topic, BroadcastMessage(topic=topic, payload=payload))
            if state.cluster is not None:
                await state.cluster.publish(topic, payload)

        case Heartbeat():
            state.presence.touch(conn)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Drive one connection for its whole lifetime.

    Authenticates *before* accepting — a missing or wrong `?token=` never gets a
    socket. (Starlette answers a pre-accept close with HTTP 403 rather than the
    401 the Rust version could return; the upgrade is refused either way, which
    is the criterion that matters.)
    """
    state = cast(AppState, websocket.app.state.app_state)

    try:
        query = WsQuery.model_validate(dict(websocket.query_params))
    except ValidationError:
        await websocket.close(code=1008, reason="bad query")
        return

    expected = state.settings.ws_auth_token.get_secret_value()
    # An empty configured token means the server itself is misconfigured — fail
    # closed rather than treating it as "auth disabled".
    if not expected or query.token != expected:
        await websocket.close(code=1008, reason="unauthorized")
        return

    await websocket.accept()

    conn = next_conn_id()
    identity = resolve_identity(query.identity, conn)
    mailbox = state.new_mailbox()
    log.info("websocket connected", conn=conn_label(conn), identity=identity)

    writer = asyncio.create_task(_writer(websocket, mailbox), name=f"ws-writer-{conn}")
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                command = client_message_adapter.validate_json(raw)
            except ValidationError as exc:
                # Reject the frame, keep the socket: a malformed message is a
                # client bug, not a reason to drop a working connection (SPEC
                # protocol checklist).
                mailbox.deliver(ErrorMessage(reason=f"invalid message: {_brief(exc)}"))
                continue
            await dispatch(state, conn, identity, mailbox, command)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - teardown must run whatever happened
        log.debug("websocket receive error", conn=conn_label(conn), error=str(exc))
    finally:
        # Teardown — runs no matter how we exited (clean close, protocol error,
        # abrupt drop, cancellation at shutdown). This block is the *only*
        # reason a dropped socket leaves nothing behind, so nothing in it may
        # raise past the first statement.
        state.hub.disconnect(conn)
        state.presence.disconnect(conn)
        mailbox.close()
        # Let the writer flush what is already queued, then stop waiting on it.
        # A writer stuck on a socket that will never drain must not hold the
        # connection's teardown (or, at shutdown, the whole process) open.
        try:
            await asyncio.wait_for(writer, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            writer.cancel()
        log.info("websocket disconnected", conn=conn_label(conn))


def _brief(exc: ValidationError) -> str:
    """One short line from a pydantic error, for the client-facing `error` frame.

    Deliberately lossy: the full `ValidationError` repr carries the input value
    back to the client and can run to hundreds of characters, which makes an
    error frame a nice amplification primitive.
    """
    first = exc.errors()[0] if exc.errors() else None
    if first is None:  # pragma: no cover - pydantic always yields at least one
        return "malformed frame"
    location = ".".join(str(part) for part in first["loc"]) or "frame"
    return f"{location}: {first['msg']}"
