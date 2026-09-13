"""The LL-HLS delivery surface — **wired**, not a vertical.

The routing, path guards, content types, cache headers and blocking-reload
parameter parsing are done. What the handlers call into is where the verticals
live: the playlist renders through V4 (`llhls.py`), and the bytes behind every
media route were built by V3 (`fmp4.py`) and pushed by V2 (`session.py`). Until
a publisher gets that far there is no stream, so every media route is a clean
`404` and `GET /live` is empty.

## The cache-header ladder

Each resource tells a CDN the truth about how long its bytes stay valid:

| route                 | changes            | Cache-Control                         |
| --------------------- | ------------------ | ------------------------------------- |
| `index.m3u8`          | every part         | `no-store`                            |
| `part/{msn}/{i}.m4s`  | never; evicted soon| `public, max-age=5`                   |
| `seg/{msn}.m4s`       | never              | `public, max-age=31536000, immutable` |
| `init.mp4`            | never (byte-stable)| same                                  |

The two `immutable` rows are only honest because an msn is never reused and V3
builds a byte-stable init. The caching checklist also wants a stable `ETag` on
them — not added here, and yours.

## Path segments are hostile input

`key` is checked by `safe_key` before any lookup. Starlette matches routes
against the percent-*decoded* path, so `..%2F..` becomes `../..` and never
matches a single `{key}` segment at all — but an empty key, `.`, `..`, a NUL or
a backslash can still arrive, and are refused with a `400`. The store is a dict,
so there is no filesystem to escape into today; the guard is what keeps that
true the day someone adds a disk cache.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response

from . import llhls
from .errors import BadRequestError, NotFoundError, NotReadyError
from .live import LiveStream
from .state import AppState, get_state

__all__ = ["router"]

router = APIRouter()

HLS_PLAYLIST = "application/vnd.apple.mpegurl"
MP4_INIT = "video/mp4"
MP4_SEGMENT = "video/iso.segment"

IMMUTABLE = "public, max-age=31536000, immutable"
SHORT_LIVED = "public, max-age=5"

State = Annotated[AppState, Depends(get_state)]
Index = Annotated[int, Path(ge=0)]


def safe_key(key: str) -> str:
    """Reject a stream key that could escape the store. Unknown-but-safe keys
    are the caller's `404`, not this function's business."""
    if key in ("", ".", "..") or any(ch in key for ch in "/\\\0"):
        raise BadRequestError("invalid stream key")
    return key


StreamKey = Annotated[str, Depends(safe_key)]


def _stream(state: AppState, key: str) -> LiveStream:
    stream = state.registry.get(key)
    if stream is None:
        raise NotFoundError()
    return stream


def _media(body: bytes, media_type: str, cache_control: str) -> Response:
    # `body` is the window's own `bytes` object, handed over by reference — see
    # `live.py` on why that is the fan-out contract.
    return Response(body, media_type=media_type, headers={"Cache-Control": cache_control})


@router.get("/healthz", response_class=Response)
async def healthz() -> Response:
    """Liveness: the process is up and serving."""
    return Response("ok", media_type="text/plain")


@router.get("/live")
async def list_live(state: State) -> dict[str, list[str]]:
    """The stream keys currently on air."""
    return {"live": state.registry.live_keys()}


@router.get("/status")
async def status(state: State) -> dict[str, Any]:
    """A small, non-secret view of the server.

    `last_session_failure` is the useful field on a scaffold: when a publisher
    connection reaches a vertical that raises `NotImplementedError`, this names
    the function, without a trip to the log. No stream keys appear here —
    `live_streams` is a count, not the list (that is `GET /live`, which a player
    needs; this is for operators).
    """
    settings = state.settings
    return {
        "rtmp_port": state.ingest.port,
        "rtmp_sessions": state.ingest.active_sessions,
        "live_streams": len(state.registry),
        "last_session_failure": state.ingest.last_failure,
        "open_ingest": not settings.allowed_keys,
        "target_part_secs": settings.target_part_secs,
        "target_segment_secs": settings.target_segment_secs,
        "live_window_segments": settings.live_window_segments,
    }


@router.get("/live/{key}/index.m3u8", response_class=Response)
async def media_playlist(
    state: State,
    key: StreamKey,
    msn: Annotated[int | None, Query(alias="_HLS_msn", ge=0)] = None,
    part: Annotated[int | None, Query(alias="_HLS_part", ge=0)] = None,
    skip: Annotated[str | None, Query(alias="_HLS_skip")] = None,
) -> Response:
    """The LL-HLS media playlist, with blocking reload (V4)."""
    stream = _stream(state, key)
    params = llhls.ReloadParams(msn=msn, part=part, skip=skip == "YES")
    body = await llhls.media_playlist(
        stream,
        params,
        part_target=state.settings.target_part_secs,
    )
    return Response(body, media_type=HLS_PLAYLIST, headers={"Cache-Control": "no-store"})


@router.get("/live/{key}/init.mp4", response_class=Response)
async def init_segment(state: State, key: StreamKey) -> Response:
    """The CMAF init segment — `503` until V3 has built it, then immutable."""
    stream = _stream(state, key)
    if stream.init is None:
        raise NotReadyError()
    return _media(stream.init, MP4_INIT, IMMUTABLE)


@router.get("/live/{key}/seg/{msn}.m4s", response_class=Response)
async def segment(state: State, key: StreamKey, msn: Index) -> Response:
    """One complete media segment — `404` while forming or once evicted."""
    body = _stream(state, key).segment_bytes(msn)
    if body is None:
        raise NotFoundError()
    return _media(body, MP4_SEGMENT, IMMUTABLE)


@router.get("/live/{key}/part/{msn}/{part}.m4s", response_class=Response)
async def partial_segment(state: State, key: StreamKey, msn: Index, part: Index) -> Response:
    """One part — short-lived, because it falls out of the window in seconds."""
    body = _stream(state, key).part_bytes(msn, part)
    if body is None:
        raise NotFoundError()
    return _media(body, MP4_SEGMENT, SHORT_LIVED)
