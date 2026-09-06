"""The media library and the packaging pipeline wiring.

This is **plumbing** — it is fully implemented. It scans `MEDIA_DIR` at startup
into an in-memory catalog of assets -> renditions -> source files, and it composes
the vertical building blocks into the outputs the HTTP layer serves:

    demux (V1) -> plan_segments (V2) -> { init/media segment (V2), manifests (V3) }

Every method here reads a source and calls into `isobmff` / `segment` /
`manifest`, whose interesting bodies raise `NotImplementedError`. So a real
request walks this wiring and lands on the first unbuilt vertical — that
exception, and the vertical it names, is your worklist.

## Why the source is `mmap`ped and not read

The obvious implementation is `path.read_bytes()`. It is also the one that makes
V2's memory criterion unreachable: a 4 GB movie becomes 4 GB of RSS before a
single segment is cut, and no amount of care in the segmenter can undo that.

`mmap` maps the file into the address space instead. The kernel pages in only the
regions actually touched, `memoryview` slices of it cost nothing, and the pages
are evictable under pressure — so cutting segment 300 of a long asset touches the
sample tables and that segment's bytes, and nothing else. This is the Python
answer to a problem Rust solved with `Bytes` and careful slicing, and it is a
better answer than the one the Rust scaffold used.

The catch, and it is a real one: a page fault is *blocking I/O that looks like a
memory read*. There is no `await` at the point it happens and no way to make one,
which is exactly why everything below runs inside `asyncio.to_thread`.

## Why every public method is `to_thread`

Demuxing, planning and muxing are synchronous CPU-bound work over a mapped file.
Run any of it directly in a handler and it blocks the event loop for its whole
duration — every other in-flight request, including the health check, stops. So
each method here is a thin `async` wrapper around a `_sync` implementation
dispatched to the default thread pool.

Be clear-eyed about what that buys and what it does not. It keeps the loop
responsive, which is the SPEC's checklist item and the difference between a
server that is slow and one that is hung. It does **not** buy parallel muxing: the
GIL means two threads cutting two segments interleave rather than overlap.
CPython releases the GIL around the file I/O and the page faults, so concurrent
*cold* segment cuts do overlap partially — measure how much rather than assuming,
and record it in `docs/11-benchmarks.md`. If muxing throughput turns out to be
the wall, the escalation ladder is: memoize aggressively (below), then a process
pool, then move the inner byte-shuffling loops out of Python. Finding where that
wall is *is* the Python version of this project's boss fight.

## Why an unknown asset cannot escape the library

The catalog is an **allowlist**, and that is the whole traversal defence. Names
that arrive in a URL are never joined onto a path — they are used as `dict` keys
against a mapping built by scanning the directory at startup. A name that was not
scanned has no entry, so it is a `KeyError` and a clean 404, and there is no code
path in which a request-supplied string reaches the filesystem. `..` does not
need to be rejected; it simply is not a key. `routes.guard_name` rejects the
obviously hostile shapes as well, but the containment property comes from here.
"""

from __future__ import annotations

import asyncio
import mmap
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from .errors import SegmentOutOfRange, UnknownAsset, UnknownRendition
from .isobmff import MediaInfo, Track, demux
from .manifest import RenditionInfo, dash_mpd, hls_master_playlist, hls_media_playlist
from .segment import SegmentIndex, build_init_segment, build_media_segment, plan_segments

__all__ = ["Asset", "Catalog", "Rendition"]

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class Rendition:
    """One rung of an asset's bitrate ladder: a source file on disk."""

    id: str
    """Rendition id — the source filename stem, e.g. `720p`."""

    source_path: Path
    """Absolute path to the source `.mp4`. Built at scan time from a real
    directory entry, never from anything a request supplied."""

    bandwidth: int = 0
    width: int = 0
    height: int = 0
    """TODO(V3/V4): fill these from the demuxed track — probe each source once at
    load, or lazily on first use — so the master playlist advertises a real
    ladder. Left at 0 so the scaffold contains no fabricated numbers, and note
    that `BANDWIDTH=0` in a master playlist is worse than useless: a player reads
    it as "this rung is free" and picks it every time."""


@dataclass(slots=True)
class Asset:
    """One asset (a single title) and its renditions, keyed by rendition id."""

    name: str
    renditions: dict[str, Rendition] = field(default_factory=dict[str, Rendition])


@contextmanager
def _mapped(path: Path) -> Generator[memoryview]:
    """Map a source file read-only and hand out a view over it.

    The view — and every slice taken from it — must be gone before the mapping
    closes, which is what the `release()` in the `finally` enforces. If you see
    `BufferError: cannot close exported pointers exist` coming out of here, it
    means something upstream retained a slice of the source past the end of the
    request: check that `CodecConfig.setup` is `bytes` and not a `memoryview`.
    That error is a feature. The alternative — a view into an unmapped page — is a
    segfault.
    """
    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
            view = memoryview(mapping)
            try:
                yield view
            finally:
                view.release()


class Catalog:
    """The media library plus the segmenting target."""

    def __init__(self, assets: dict[str, Asset], media_dir: Path, target_segment_secs: float):
        self._assets = assets
        self._media_dir = media_dir
        self._target_segment_secs = target_segment_secs

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(cls, media_dir: Path, target_segment_secs: float) -> Catalog:
        """Scan `MEDIA_DIR/<asset>/<rendition>.mp4` into an in-memory catalog.

        Synchronous, and called from the lifespan via `asyncio.to_thread`: this
        is blocking directory I/O, and it happens once before the server accepts
        traffic. A missing `MEDIA_DIR` is created rather than fatal, so the server
        still starts and `GET /assets` answers with an empty library — which is a
        much better first-run experience than a crash telling you to mkdir.
        """
        media_dir.mkdir(parents=True, exist_ok=True)
        assets: dict[str, Asset] = {}

        for asset_dir in sorted(media_dir.iterdir()):
            if not asset_dir.is_dir():
                continue
            renditions = {
                source.stem: Rendition(id=source.stem, source_path=source)
                for source in sorted(asset_dir.glob("*.mp4"))
                if source.is_file()
            }
            # TODO(security, horizontal): a symlink inside MEDIA_DIR can still
            # point outside it. The allowlist stops a *request* from escaping (see
            # the module docstring), but it does not stop the library itself from
            # containing a link to /etc. Decide whether to resolve each source and
            # require `media_dir` to be one of its parents, or to refuse symlinks
            # outright, and record the choice in docs/11-design.md.
            if not renditions:
                continue
            assets[asset_dir.name] = Asset(name=asset_dir.name, renditions=renditions)
            logger.info("loaded asset", asset=asset_dir.name, renditions=len(renditions))

        logger.info("media library scanned", dir=str(media_dir), assets=len(assets))
        return cls(assets, media_dir, target_segment_secs)

    # -- read-only views of the library -------------------------------------

    @property
    def media_dir(self) -> Path:
        return self._media_dir

    def asset_names(self) -> list[str]:
        """Asset names, sorted — for `GET /assets`."""
        return sorted(self._assets)

    def rendition_ids(self, asset: str) -> list[str] | None:
        """Rendition ids for an asset, or `None` if the asset is unknown."""
        found = self._assets.get(asset)
        return sorted(found.renditions) if found is not None else None

    # -- lookups (plumbing: map missing -> the right 404) --------------------

    def _asset(self, asset: str) -> Asset:
        try:
            return self._assets[asset]
        except KeyError:
            raise UnknownAsset() from None

    def _rendition(self, asset: str, rendition: str) -> Rendition:
        try:
            return self._asset(asset).renditions[rendition]
        except KeyError:
            raise UnknownRendition() from None

    # -- packaging pipeline (composes the verticals) -------------------------
    #
    # TODO(caching, horizontal): every call below re-maps and re-demuxes the
    # source, so a player pulling 300 segments demuxes the asset 300 times.
    # Memoize the `MediaInfo`, the `SegmentIndex`, and the built init/segment
    # bytes (keyed by asset/rendition[/index]) so a hot segment is cut once
    # rather than per request — that is the SPEC's memoization criterion, and it
    # is also what makes a stable `ETag` worth anything. Two things to get right
    # when you do: bound the cache (a whole asset's segments will not fit in RAM,
    # and an unbounded dict here is a memory leak with a slow fuse), and make it
    # safe for concurrent requests for the *same* cold segment, or a thundering
    # herd will cut it N times in N threads.

    def master_playlist(self, asset: str) -> str:
        """HLS master playlist for an asset — the ABR ladder (V3/V4).

        Synchronous and not threaded, uniquely among these: it reads only the
        catalog's own metadata and touches no media, so there is nothing to
        block on.
        """
        found = self._asset(asset)
        renditions = [
            RenditionInfo(
                id=rendition.id,
                bandwidth=rendition.bandwidth,
                width=rendition.width,
                height=rendition.height,
                uri=f"{rendition.id}/index.m3u8",
            )
            for rendition in sorted(found.renditions.values(), key=lambda r: r.id)
        ]
        return hls_master_playlist(renditions)

    async def media_playlist(self, asset: str, rendition: str) -> str:
        """HLS media playlist for one rendition (V1 -> V2 -> V3)."""
        source = self._rendition(asset, rendition)
        return await asyncio.to_thread(self._media_playlist_sync, source)

    async def dash_manifest(self, asset: str, rendition: str) -> str:
        """DASH MPD for one rendition (V1 -> V2 -> V3)."""
        source = self._rendition(asset, rendition)
        return await asyncio.to_thread(self._dash_manifest_sync, source)

    async def init_segment(self, asset: str, rendition: str) -> bytes:
        """CMAF init segment for one rendition (V1 -> V2)."""
        source = self._rendition(asset, rendition)
        return await asyncio.to_thread(self._init_segment_sync, source)

    async def media_segment(self, asset: str, rendition: str, index: int) -> bytes:
        """One media segment for one rendition (V1 -> V2)."""
        source = self._rendition(asset, rendition)
        return await asyncio.to_thread(self._media_segment_sync, source, index)

    # -- the synchronous implementations, run in the thread pool -------------

    def _plan(self, data: memoryview) -> tuple[MediaInfo, Track, SegmentIndex]:
        """demux (V1) -> primary video track -> segmentation plan (V2)."""
        media = demux(data)
        track = media.primary_video()
        return media, track, plan_segments(track, self._target_segment_secs)

    def _media_playlist_sync(self, rendition: Rendition) -> str:
        with _mapped(rendition.source_path) as data:
            _, track, index = self._plan(data)
            return hls_media_playlist(index, track)

    def _dash_manifest_sync(self, rendition: Rendition) -> str:
        with _mapped(rendition.source_path) as data:
            _, track, index = self._plan(data)
            return dash_mpd(index, track)

    def _init_segment_sync(self, rendition: Rendition) -> bytes:
        with _mapped(rendition.source_path) as data:
            media = demux(data)
            return build_init_segment(media.primary_video())

    def _media_segment_sync(self, rendition: Rendition, index: int) -> bytes:
        with _mapped(rendition.source_path) as data:
            _, track, plan = self._plan(data)
            if not 0 <= index < len(plan.segments):
                raise SegmentOutOfRange()
            return build_media_segment(data, track, plan.segments[index])
