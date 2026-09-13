"""The app-facing HTTP/WS surface — **wired**, delegates to the vertical modules.

Three planes meet here, each handled by its vertical:

* **Ingest control** (`/ingest/*`) — the webhook an RTMP/WebRTC ingest edge
  calls when a broadcaster connects or disconnects. Drives V1's state machine.
* **Playback** (`/live/*`) — HLS master/media playlists and segments, served by
  the edge (V3).
* **Chat** (`/chat/{stream}/ws`) — the WebSocket a viewer opens to a channel (V4).

Handlers are thin: parse, then call into `control`, `edge`, or `chat`. Those
calls raise `NotImplementedError` until the vertical exists, which `errors.py`
renders as a `501` naming the todo — so every route is reachable and `curl`
tells you which vertical it is waiting on.

Two things the framework already does for you, both on the horizontal checklist:
a body missing `stream_key` is a `422` before the handler runs, and
`_HLS_msn=-1` is a `422` from the `ge=0` constraint. What it does *not* do is
check that a stream key, rendition or segment name has a safe shape — that
allowlist is yours (Security → input validation).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response, WebSocket
from pydantic import BaseModel

from .edge import PlaylistCursor, parse_range_header
from .state import AppState, get_state

__all__ = ["router"]

router = APIRouter()

State = Annotated[AppState, Depends(get_state)]

M3U8 = "application/vnd.apple.mpegurl"
SEGMENT = "video/iso.segment"
"""CMAF segment/part. An init segment is `video/mp4`; HLS correctness is yours."""


# ---- ingest control plane (V1) -------------------------------------------------


class IngestStart(BaseModel):
    stream_key: str
    """The broadcaster's secret ingest key. Never log it."""
    ingest_node: str
    """The ingest node that holds the RTMP/WebRTC connection."""


class IngestStop(BaseModel):
    stream_key: str


@router.post("/ingest/start")
async def ingest_start(body: IngestStart, state: State) -> dict[str, Any]:
    """An ingest edge reports a broadcaster connected."""
    session = await state.platform.on_ingest_start(body.stream_key, body.ingest_node)
    return session.model_dump(mode="json")


@router.post("/ingest/stop")
async def ingest_stop(body: IngestStop, state: State) -> dict[str, bool]:
    """An ingest edge reports a broadcaster disconnected."""
    await state.platform.on_ingest_stop(body.stream_key)
    return {"ok": True}


# ---- playback / edge (V3) ------------------------------------------------------


@router.get("/live/{stream}/master.m3u8", response_class=Response)
async def master_playlist(stream: str, state: State) -> Response:
    """The ABR master playlist."""
    body = await state.edge.master_playlist(stream)
    return Response(body, media_type=M3U8)


@router.get("/live/{stream}/{rendition}/index.m3u8", response_class=Response)
async def media_playlist(
    stream: str,
    rendition: str,
    state: State,
    msn: Annotated[int | None, Query(alias="_HLS_msn", ge=0)] = None,
    part: Annotated[int | None, Query(alias="_HLS_part", ge=0)] = None,
) -> Response:
    """A rendition's LL-HLS media playlist, with blocking reload.

    Registered before the segment route on purpose: routes match in order, and
    `index.m3u8` would otherwise be captured as a segment named `index.m3u8`.
    """
    cursor = PlaylistCursor(msn=msn, part=part) if msn is not None else None
    body = await state.edge.media_playlist(stream, rendition, cursor)
    return Response(body, media_type=M3U8)


@router.get("/live/{stream}/{rendition}/{segment}", response_class=Response)
async def segment(
    stream: str,
    rendition: str,
    segment: str,
    state: State,
    range_header: Annotated[str | None, Header(alias="range")] = None,
) -> Response:
    """A segment or partial, byte-range capable.

    A ranged request answers `206` with a `Content-Range`, not `200` — that
    status and header are part of V3's Range criterion, not done here yet.
    """
    byte_range = parse_range_header(range_header) if range_header is not None else None
    data = await state.edge.segment(stream, rendition, segment, byte_range)
    return Response(data, media_type=SEGMENT)


# ---- chat (V4) -----------------------------------------------------------------


@router.websocket("/chat/{stream}/ws")
async def chat_ws(websocket: WebSocket, stream: str, state: State) -> None:
    """One viewer's chat socket.

    TODO(V4): two directions at once. Outbound, pump the subscription's outbox
    into the socket; inbound, read frames and `publish` them. Each direction is
    its own coroutine, and when *either* ends — the viewer closed the tab, or the
    overflow policy evicted them — the other must stop too. An
    `asyncio.TaskGroup` gives you that shape; so does `asyncio.wait` with
    `FIRST_COMPLETED`, if you want to choose which ending is an error.

    `subscribe` guarantees the viewer leaves the channel whichever way this
    handler exits.
    """
    with state.chat.subscribe(stream):
        await websocket.accept()
        raise NotImplementedError("V4: pump the outbox → socket, publish inbound frames")
