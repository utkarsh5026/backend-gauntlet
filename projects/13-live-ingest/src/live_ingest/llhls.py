"""V4 — Low-Latency HLS playlist + blocking delivery: break the latency wall.

Module: `src/live_ingest/llhls.py`.

A regular live playlist is a rolling window of `#EXTINF` segments the player
re-fetches every target duration; its latency floor is about three segments.
LL-HLS gets under it by publishing, for the still-forming segment, one
`#EXT-X-PART` per ~200 ms part, an `#EXT-X-PRELOAD-HINT` for the part that does
not exist yet, and `#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES`. The player then
asks for `index.m3u8?_HLS_msn=N&_HLS_part=M`, and the server **holds** that
request until the named part exists — answering the instant it does.

The park-until-signalled mechanism lives in `LiveStream.await_edge` (wired, in
`live.py`). This module owns the two things that are the V4 learning:
**rendering** the playlist from a window snapshot, and the **policy** that maps a
blocking-reload request onto an edge to wait for, with a bounded wait.
`docs/03-llhls-blocking-reload.md` goes tag by tag.

## Why a held request costs almost nothing here

A parked `media_playlist` call is a suspended coroutine waiting on one
`asyncio.Event` — a few hundred bytes, no thread, no poll loop. Two hundred
viewers holding reloads are two hundred of those, all woken by the single
`_wake()` inside `push_part`. What *does* cost is the wake: every one of them
resumes on the same thread in the same tick and renders a playlist. That burst
of renders once per part is where CPython's single core shows up in the Latency
Wall's p99, and it is the first thing to look for in the profile. A renderer
that allocates less per call — or a rendered playlist memoized per edge, since
every woken viewer wants the same text — is the kind of fix the benchmarks doc
should record.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass

from .live import LiveEdge, LiveStream, WindowSnapshot

__all__ = [
    "MAX_BLOCK_SECONDS",
    "ReloadParams",
    "media_playlist",
    "render_media_playlist",
]

MAX_BLOCK_SECONDS = 5.0
"""The longest a blocking reload is held before answering with what exists, so
a bogus `_HLS_msn` far in the future cannot pin a connection open forever."""


@dataclass(frozen=True, slots=True)
class ReloadParams:
    """The LL-HLS blocking-reload query parameters."""

    msn: int | None = None
    """`_HLS_msn`: the media sequence number the playlist must include."""
    part: int | None = None
    """`_HLS_part`: the part index within that media sequence."""
    skip: bool = False
    """`_HLS_skip=YES`: the player accepts a delta playlist."""


async def media_playlist(
    stream: LiveStream,
    params: ReloadParams,
    *,
    part_target: float,
    max_block: float = MAX_BLOCK_SECONDS,
) -> str:
    """Serve the media playlist for a stream, honoring blocking reload (V4).

    The wait is wired; the rendering is not. If the player named an
    `(msn, part)` the stream has not reached, park until it has, the stream
    ends, or `max_block` elapses — never busy-poll, never 404 a part that is on
    schedule.

    TODO(V4): the mapping below is a *starting stance*, not the answer.
    `_HLS_msn` without `_HLS_part` waits for part 0 of that segment — should it
    wait for the whole segment? A part index past the end of a segment that has
    already closed will never be reached. A target far beyond the preload hint,
    or behind the window, arguably deserves an immediate answer (the RFC draft
    says `400`) instead of a five-second hold. Decide, test, and record it in
    `docs/13-design.md`.
    """
    if params.msn is not None:
        target = LiveEdge(params.msn, params.part or 0)
        edge = stream.edge
        if edge is None or edge < target:
            # `asyncio.timeout` cancels the wait and raises TimeoutError on
            # expiry; a timed-out hold still answers with the current playlist.
            with suppress(TimeoutError):
                async with asyncio.timeout(max_block):
                    await stream.await_edge(target)
    return render_media_playlist(stream.snapshot(), part_target=part_target)


def render_media_playlist(snapshot: WindowSnapshot, *, part_target: float) -> str:
    """Render the LL-HLS media playlist text from a window snapshot (V4 core).

    A pure function of its arguments, on purpose: it is unit-testable from a
    hand-built `WindowSnapshot` with no stream, no loop and no server.

    TODO(V4): emit, in order —
        #EXTM3U
        #EXT-X-VERSION:9                                   (parts need v9+)
        #EXT-X-TARGETDURATION:<ceil(longest segment secs)>
        #EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=<~3 × part_target>
        #EXT-X-PART-INF:PART-TARGET=<part_target>
        #EXT-X-MEDIA-SEQUENCE:<msn of the oldest segment in the window>
        #EXT-X-MAP:URI="init.mp4"
      then for each segment:
        #EXT-X-PROGRAM-DATE-TIME:<ISO-8601>                (at least on the first)
        #EXT-X-PART:DURATION=<d>,URI="part/<msn>/<i>.m4s"[,INDEPENDENT=YES]   per part
        #EXTINF:<secs>,  then  seg/<msn>.m4s               (only if complete)
      then, unless ended,
        #EXT-X-PRELOAD-HINT:TYPE=PART,URI="part/<msn>/<next>.m4s"
      and #EXT-X-ENDLIST if ended.

    The media sequence and the part indices must be monotonic across reloads.
    Format floats deliberately: `f"{0.30000000000000004}"` is a valid playlist
    and an ugly one, and `repr` of a float is not what the spec examples show.
    """
    raise NotImplementedError(
        "V4: render the LL-HLS media playlist (parts, preload hint, server-control)"
    )
