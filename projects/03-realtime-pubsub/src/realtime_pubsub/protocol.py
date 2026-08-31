"""The wire protocol and connection identity — the glue types every module
speaks.

This is scaffolding, not a vertical challenge: the message *shapes* are given so
the WebSocket handler, the hub, and the cluster bridge all agree on a
vocabulary. Extend it (an ack, a `history` request, app-level ping/pong) as the
SPEC's protocol checklist asks.

Python shape note: the Rust side used serde's internally-tagged enums. The
equivalent here is a **pydantic discriminated union** on the `type` field. It
buys the same two things — one `model_validate` call turns an arbitrary frame
into exactly one of the known variants, and an unknown or malformed `type` is a
`ValidationError` rather than a silently half-parsed object — plus the error
message names the offending field, which is what `routes.py` echoes back to the
client as a protocol `error`.
"""

from __future__ import annotations

from itertools import count
from typing import Annotated, Any, Literal, NewType

from pydantic import BaseModel, Field, TypeAdapter

__all__ = [
    "BroadcastMessage",
    "ClientMessage",
    "ConnId",
    "ErrorMessage",
    "Heartbeat",
    "PresenceMessage",
    "Publish",
    "ServerMessage",
    "Subscribe",
    "Topic",
    "Unsubscribe",
    "client_message_adapter",
    "conn_label",
    "next_conn_id",
]

type Topic = str

# A process-unique id for one open WebSocket connection. Used as the key a
# subscriber is tracked under in the hub and the presence registry.
#
# `NewType` over a plain `int` rather than a wrapper class: it is free at
# runtime, it stays hashable and comparable for use as a dict key, and pyright
# still refuses to let you pass a bare `int` where a ConnId belongs. The Rust
# side needed an `AtomicU64` behind this; here a module-level `itertools.count`
# is enough, because ids are only ever minted from the event loop thread and
# `next()` on a count object is atomic under the GIL anyway.
ConnId = NewType("ConnId", int)

_counter = count(1)


def next_conn_id() -> ConnId:
    """Mint the next id. Monotonic for the lifetime of the process."""
    return ConnId(next(_counter))


def conn_label(conn: ConnId) -> str:
    """Human-readable form used in logs and as the presence fallback identity."""
    return f"conn-{conn}"


# --- Client -> server ---------------------------------------------------------


class Subscribe(BaseModel):
    """Join a topic; future publishes to it will be delivered to this socket."""

    type: Literal["subscribe"]
    topic: str


class Unsubscribe(BaseModel):
    """Leave a topic."""

    type: Literal["unsubscribe"]
    topic: str


class Publish(BaseModel):
    """Broadcast `payload` to everyone subscribed to `topic`."""

    type: Literal["publish"]
    topic: str
    payload: Any


class Heartbeat(BaseModel):
    """Application-level liveness ping (`{"type":"heartbeat"}`).

    Browsers cannot send WebSocket protocol ping frames from JS, so a
    live-but-idle client sends this on a timer to prove it is still here and
    refresh its presence TTL. Carries no payload — its arrival *is* the signal.
    """

    type: Literal["heartbeat"]


type ClientMessage = Annotated[
    Subscribe | Unsubscribe | Publish | Heartbeat,
    Field(discriminator="type"),
]

# Built once at import: a TypeAdapter compiles the union's validator, and
# rebuilding it per frame would put that cost on the hot path.
client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# --- Server -> client ---------------------------------------------------------


class PresenceMessage(BaseModel):
    """The current membership of a topic (V3)."""

    type: Literal["presence"] = "presence"
    topic: str
    members: list[str]


class BroadcastMessage(BaseModel):
    """A broadcast delivered on a topic this client subscribes to."""

    type: Literal["message"] = "message"
    topic: str
    payload: Any


class ErrorMessage(BaseModel):
    """A protocol-level error (bad frame, over a limit, ...)."""

    type: Literal["error"] = "error"
    reason: str


type ServerMessage = BroadcastMessage | PresenceMessage | ErrorMessage
