"""V3 — LL-HLS edge delivery with request coalescing. `src/live_platform/edge.py`.

Between the packager (origin) and thousands of viewers sits an edge. Its whole
job is to serve the *same* freshly-produced bytes to a crowd without melting the
origin — and to do it at **low latency**, which is where LL-HLS gets subtle:

1. **Single-flight on a cold segment.** The instant a new part is referenced by
   the playlist, every viewer requests it at once. If the edge does not have it
   yet, exactly **one** fill should go to origin while the rest *wait on that
   fill* — a thundering herd / cache stampede, now on video. (The same failure
   the URL shortener's boss taught, one tier up.)
2. **Blocking playlist reload.** LL-HLS players long-poll the media playlist with
   `_HLS_msn` / `_HLS_part`: "hold the request until media-sequence N part K
   exists, then return." That held-open read is what pulls glass-to-glass toward
   ~2 s — and done wrong it either busy-polls or returns stale.

## Single-flight, in Python

The Rust tracked in-flight keys in a `HashSet` behind a mutex. The Python shape
is a `dict` from key to an `asyncio.Future[bytes]`: the first request for a cold
key *creates* the future and performs the one fill; every request arriving while
it is pending finds the future already there and awaits the same one. No lock is
needed — a dict lookup and insert never yield, so "is it in flight? if not,
register it" cannot be interleaved, *provided no `await` sits between them*.

Three ways this goes wrong quietly in Python, each worth its own test:

* **One viewer's disconnect cancels everyone's fill.** When a client goes away its
  request task is cancelled, and cancellation propagates into whatever that task
  is awaiting. If that is the fill itself, the 999 viewers waiting on the same
  future inherit the `CancelledError`. Look at what `asyncio.shield` protects,
  and from whom.
* **A failed fill poisons the key.** If the entry is removed only on success, the
  first origin timeout leaves a future that fails, forever, for everyone.
* **"Future exception was never retrieved".** A future that fails with nobody
  awaiting it logs that at garbage collection. Harmless — until it is the only
  symptom of an origin outage you did not otherwise see.

## Blocking reload, without polling

"Hold the request until part K exists" is a *wait on a condition*, and asyncio has
one: `asyncio.Condition`, whose `wait_for(predicate)` sleeps until someone calls
`notify_all()`, then re-checks. Pair it with `asyncio.timeout` for the deadline.
What must never appear is `while True: …; await asyncio.sleep(0.05)` — a
busy-poll with extra steps, which at 100k viewers is two million wakeups a second
doing nothing.

Scaffold state: the edge is constructed over a pooled origin client and an empty
in-flight map; the serve paths are the V3 worklist.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

__all__ = ["ByteRange", "EdgeCache", "PlaylistCursor", "parse_range_header"]


@dataclass(frozen=True, slots=True)
class PlaylistCursor:
    """The part of a media playlist a blocking reload waits for.

    From `_HLS_msn` / `_HLS_part`. A `part` without an `msn` is meaningless in
    LL-HLS, which is why the route only builds a cursor when `msn` is present.
    """

    msn: int
    part: int | None = None


@dataclass(frozen=True, slots=True)
class ByteRange:
    """An HTTP byte range, both ends inclusive as RFC 9110 defines them.

    `end` is `None` for an open-ended `bytes=500-`. Suffix ranges (`bytes=-500`,
    "the last 500 bytes") need the object's length to resolve; whether to
    support them is yours to decide.
    """

    start: int
    end: int | None = None


def parse_range_header(value: str) -> ByteRange | None:
    """Parse a `Range` header into a `ByteRange`, or `None` if it is unusable.

    TODO(V3): players seek and resume with these. Everything about the header is
    client-controlled: a reversed range, a multi-range list, an `end` past the
    object, garbage. Decide which of those is a `416`, which is "ignore the header
    and send the whole object" (what RFC 9110 permits), and which is a `400`.
    """
    raise NotImplementedError("V3: parse `bytes=start-end` into a ByteRange")


class EdgeCache:
    """The edge cache in front of the packager origin."""

    def __init__(self, http: httpx.AsyncClient, *, origin_base: str) -> None:
        self._http = http
        """Pooled client for origin fills — keep-alive reuse across misses."""
        self._origin_base = origin_base.rstrip("/")
        self._inflight: dict[str, asyncio.Future[bytes]] = {}
        """Cold keys currently being filled from origin: the single-flight map.
        See the module docstring for the three ways it leaks or poisons."""

    @property
    def origin_base(self) -> str:
        """Base URL of the packager origin, e.g. `http://packager:9000`."""
        return self._origin_base

    # ---- V3 worklist: blocking reload + single-flight fan-out ------------------

    async def master_playlist(self, stream_key: str) -> str:
        """The master playlist for a stream: the ABR renditions a player picks from.

        TODO(V3): cacheable and cheap — it only changes when the ladder does. The
        interesting latency work is the media playlist below. Fetch-through from
        origin on a miss.
        """
        raise NotImplementedError("V3: return the master m3u8 (fetch-through, then cache)")

    async def media_playlist(
        self, stream_key: str, rendition: str, cursor: PlaylistCursor | None
    ) -> str:
        """A rendition's media playlist, with LL-HLS blocking reload.

        TODO(V3): with no cursor, serve the current playlist. With one naming a
        part that does not exist yet, hold the request open until it is produced
        (or a deadline passes), then return the updated playlist — never
        busy-poll, and never return a playlist stale past the requested cursor.
        """
        raise NotImplementedError("V3: blocking reload — await the msn/part, then serve")

    async def segment(
        self,
        stream_key: str,
        rendition: str,
        name: str,
        byte_range: ByteRange | None,
    ) -> bytes:
        """A segment or partial segment's bytes.

        TODO(V3): on a cold key, single-flight the origin fill through
        `_inflight` so a crowd racing for the same just-produced part triggers
        **one** fill and every other request awaits it. Honour `byte_range` for
        seeking. A slow or dead origin must come back as `UpstreamError` /
        `UpstreamTimeoutError`, not as a hung request.
        """
        raise NotImplementedError("V3: single-flight the fill, fan the same bytes to waiters")
