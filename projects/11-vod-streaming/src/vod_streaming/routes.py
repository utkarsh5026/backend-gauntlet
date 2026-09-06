"""HTTP surface: the HLS/DASH endpoints a player hits.

Routing, name validation, content types and CORS are wired. What the handlers
call into — `catalog.*`, which composes `isobmff` / `segment` / `manifest` — is
where the `NotImplementedError`s live. Run this as-is and `GET /healthz`,
`GET /assets` and `GET /metrics` work; the first playlist or segment request
raises, naming the vertical it needs. That message is the worklist.

## The URL shape is a contract with the manifest

`seg/{index}` here has to match exactly what `manifest.hls_media_playlist` writes
under each `#EXTINF`, and `init.mp4` has to match its `#EXT-X-MAP:URI`. Those are
relative URIs, resolved against the playlist's own URL — which is why the media
playlist living at `/vod/{asset}/{rendition}/index.m3u8` is what makes a bare
`seg/0` resolve to `/vod/{asset}/{rendition}/seg/0`. Move one and you must move
the other, and the failure mode is a player fetching 404s in a loop.

## Content types are not cosmetic

Safari refuses a playlist served as `text/plain`, and `hls.js` decides how to
handle a segment partly from its type. The four constants below are the
Protocols checklist item, and the cheapest one in the project to earn.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path
from fastapi.responses import Response

from . import delivery
from .errors import InvalidRequest
from .state import AppState, get_state

__all__ = ["guard_name", "router"]

HLS_PLAYLIST = "application/vnd.apple.mpegurl"
DASH_MPD = "application/dash+xml"
INIT_SEGMENT = "video/mp4"
MEDIA_SEGMENT = "video/iso.segment"

StateDep = Annotated[AppState, Depends(get_state)]
RangeHeader = Annotated[str | None, Header(alias="Range")]
SegmentIndex = Annotated[int, Path(ge=0)]

router = APIRouter()


def guard_name(name: str) -> str:
    """Reject an asset/rendition path segment that has no business on a filesystem.

    Defence in depth, not the defence itself: `catalog` never joins a
    request-supplied string onto a path — it looks names up in a dict built by
    scanning the library — so an unknown or hostile name is already a clean 404 by
    construction. See `catalog`'s module docstring.

    This exists anyway because the cost is four comparisons and the failure it
    guards against is the one nobody notices being introduced: the day someone
    adds a "just read the file directly" shortcut, this is already in front of it.
    Starlette will not match a `/` inside a single path parameter, but an empty
    name, `.`, `..`, a NUL or a backslash can all still arrive.
    """
    if not name or name in {".", ".."} or any(char in name for char in "/\\\0"):
        raise InvalidRequest(f"invalid path segment: {name!r}")
    return name


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness only.

    Answers `ok` with an empty library and with every vertical unbuilt, because
    the process is alive and correctly reporting what it cannot do. A readiness
    check that went red on an empty `MEDIA_DIR` would have an orchestrator restart
    a server whose only problem is that nobody has copied a video in yet.
    """
    return {"status": "ok"}


@router.get("/assets")
async def list_assets(state: StateDep) -> dict[str, Any]:
    """`GET /assets` — the loaded library (asset -> rendition ids).

    Wired and working before any vertical exists, which makes it the fastest way
    to confirm `MEDIA_DIR` points where you think it does.
    """
    catalog = state.catalog
    return {
        "assets": [
            {"asset": name, "renditions": catalog.rendition_ids(name)}
            for name in catalog.asset_names()
        ]
    }


@router.get("/vod/{asset}/master.m3u8", include_in_schema=False)
async def master_playlist(state: StateDep, asset: str) -> Response:
    """HLS master playlist — the ABR ladder (V3/V4)."""
    body = state.catalog.master_playlist(guard_name(asset))
    return delivery.text_response(body, HLS_PLAYLIST)


@router.get("/vod/{asset}/{rendition}/index.m3u8", include_in_schema=False)
async def media_playlist(state: StateDep, asset: str, rendition: str) -> Response:
    """HLS media playlist for one rendition (V3)."""
    body = await state.catalog.media_playlist(guard_name(asset), guard_name(rendition))
    return delivery.text_response(body, HLS_PLAYLIST)


@router.get("/vod/{asset}/{rendition}/manifest.mpd", include_in_schema=False)
async def dash_manifest(state: StateDep, asset: str, rendition: str) -> Response:
    """DASH MPD over the same segments (V3)."""
    body = await state.catalog.dash_manifest(guard_name(asset), guard_name(rendition))
    return delivery.text_response(body, DASH_MPD)


@router.get("/vod/{asset}/{rendition}/init.mp4", include_in_schema=False)
async def init_segment(
    state: StateDep,
    asset: str,
    rendition: str,
    range_header: RangeHeader = None,
) -> Response:
    """CMAF init segment (V2), served with `Range` like any other media (V4).

    A player rarely range-requests the init segment, but it is media and it goes
    through the same path — which is the point. `Accept-Ranges` on this response
    is part of how a client learns the media under this server is seekable at all.
    """
    body = await state.catalog.init_segment(guard_name(asset), guard_name(rendition))
    return delivery.serve_ranged(body, INIT_SEGMENT, range_header)


@router.get("/vod/{asset}/{rendition}/seg/{index}", include_in_schema=False)
async def media_segment(
    state: StateDep,
    asset: str,
    rendition: str,
    index: SegmentIndex,
    range_header: RangeHeader = None,
) -> Response:
    """One media segment (V2), served with HTTP `Range` (V4).

    `index` is a non-negative `int`, so `seg/abc` and `seg/-1` are rejected by
    FastAPI's own validation before any of this runs — as a `422`, which is a 4xx
    but not the `400` the security checklist names. Decide whether that is close
    enough or whether you want a `RequestValidationError` handler that renders
    `400`, and record the choice in `docs/11-design.md`. An index that parses but
    is past the end of the plan is a different thing entirely, and becomes a `404`
    in the catalog.
    """
    body = await state.catalog.media_segment(guard_name(asset), guard_name(rendition), index)
    return delivery.serve_ranged(body, MEDIA_SEGMENT, range_header)
