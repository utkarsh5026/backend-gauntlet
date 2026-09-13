"""V2 (part 2) — the publisher session state machine.

Module: `src/live_ingest/session.py`.

One accepted RTMP connection is one `PublishSession`. It drives the connection
from "just handshook" through the AMF0 command dance (`connect` → `createStream`
→ `publish`) to "streaming media", using the chunk reader (V1, `rtmp.py`) for
framing and the AMF0 codec (V2, `amf.py`) for the commands. Once publishing,
each audio/video message is an FLV tag (`flv.py`) whose sequence headers become
the codec config and whose frames become samples for the live packager (V3,
`fmp4.py`), which pushes built parts into the shared window (`live.py`).

`run` — the lifecycle — is **wired**. `handle` — answering each command,
enforcing the transitions, and the media path — is the worklist.
`docs/05-the-publisher-session-lifecycle.md` walks a whole connection through it.

## A state machine, and why the gate is where it is

`SessionState` is the design: media is legal only in `PUBLISHING`, and the only
way into `PUBLISHING` is a `publish` whose stream key `LiveRegistry.authorize`
accepts. That gate is the security boundary — an ingest that accepts media
before (or without) the key check lets anyone broadcast as anyone. Every other
rule is protocol hygiene; that one is an incident.

## One coroutine per connection, and what that buys

`ingest.py` runs each session as its own task. Its attributes (`state`,
`stream_key`, `live`) are touched only by that task, so they need no lock; what
it shares with every other session is the registry, which `live.py` explains is
safe on one loop. A session that raises ends *its* task and closes *its*
socket — the failure domain is one broadcaster, which is the SPEC's "a bad value
ends that session, nothing else".
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

import structlog

from .config import Settings
from .live import LiveRegistry, LiveStream
from .rtmp import ChunkStreamReader, Message, handshake

__all__ = ["PublishSession", "SessionState"]

logger = structlog.get_logger(__name__)


class SessionState(StrEnum):
    """Where a publisher connection is in its lifecycle.

    A `StrEnum` so the value logs and renders as a readable word.
    """

    CONNECTED = "connected"
    """Handshake done; waiting for `connect`."""
    APP_CONNECTED = "app_connected"
    """`connect` answered; waiting for `createStream`."""
    STREAM_CREATED = "stream_created"
    """`createStream` answered with a stream id; waiting for `publish`."""
    PUBLISHING = "publishing"
    """`publish` accepted for an authorized key; media is flowing."""


class PublishSession:
    """One publisher connection: its socket, its progress, and where its video goes."""

    def __init__(
        self,
        session_id: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        registry: LiveRegistry,
        settings: Settings,
    ) -> None:
        self.id = session_id
        self.reader = reader
        self.writer = writer
        self.registry = registry
        self.settings = settings
        self.chunks = ChunkStreamReader()
        self.state = SessionState.CONNECTED
        self.stream_key: str | None = None
        """Set once `publish` is authorized. A credential: never log it raw."""
        self.live: LiveStream | None = None
        """The shared window this session publishes into, once publishing.
        `None` until then — an unauthorized session has nowhere to push media."""

    async def run(self) -> None:
        """The whole life of one connection: handshake, message loop, teardown.

        Wired. Returns normally only if `handle` asks to stop; a closed socket
        or a protocol error propagates, and `ingest.py` classifies it.
        """
        await handshake(self.reader, self.writer)
        logger.info("handshake complete")
        try:
            while True:
                message = await self.chunks.read_message(self.reader)
                await self.handle(message)
        finally:
            # Publisher gone, for whatever reason: close out its stream so held
            # blocking reloads wake and the playlist gains #EXT-X-ENDLIST.
            if self.live is not None:
                self.live.mark_ended()
            if self.stream_key is not None:
                self.registry.close(self.stream_key)

    async def handle(self, message: Message) -> None:
        """Dispatch one reassembled RTMP message (V2, feeding V3).

        TODO(V2): route on `message.type_id` (`MessageType`):
          - `AMF0_COMMAND`: `amf.decode` the payload; the first value is the
            command name, the second the transaction id to echo back.
              `connect`      → Window Ack Size + Set Peer Bandwidth, then a
                               `_result` (`NetConnection.Connect.Success`,
                               `objectEncoding` = `amf.OBJECT_ENCODING_AMF0`);
                               → `APP_CONNECTED`
              `createStream` → `_result` carrying a stream id
                               (`MessageStreamId.DEFAULT`); → `STREAM_CREATED`
              `publish`      → the key is the argument after the null command
                               object; `registry.authorize` it — refuse with
                               `UnauthorizedError` — then `registry.open`, reply
                               `onStatus` `NetStream.Publish.Start` on the
                               stream id, → `PUBLISHING`
              anything else  → `releaseStream`, `FCPublish` and commands you
                               have never met: ignore, never crash
          - `AUDIO` / `VIDEO`: only in `PUBLISHING` (otherwise `StateError`).
            Parse with `flv.py`: the first sequence headers build the codec
            config → `fmp4.build_init` → `live.set_init`; later tags become
            `fmp4.Sample`s for a `Fragmenter` whose parts go to
            `live.push_part` (V3).
          - protocol control (window ack, user control, …) and `AMF0_DATA`:
            handle or ignore per spec.

        Replies go back through the chunk layer: AMF0 bytes wrapped in a
        type-20 message, chunked onto `self.writer`, then `await
        self.writer.drain()`. How much of a chunk *writer* you build is part of
        this vertical. An out-of-order or duplicate command must not corrupt
        state — reject it or ignore it, and document which in
        `docs/13-design.md`.
        """
        raise NotImplementedError(
            "V2/V3: handle command/media messages + drive the publish state machine"
        )
