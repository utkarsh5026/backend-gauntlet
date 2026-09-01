"""The HTTP control plane: add torrents, inspect progress, health, metrics.

Deliberately thin. The *product* is the BitTorrent engine — the raw-TCP peer
wire and the raw-UDP tracker protocol — and this router is only how you drive
and observe it. It is fully wired and answers on the bare scaffold; the handlers
that reach into the engine trip V2 inside the metainfo parser, which is where
the worklist starts.

Keeping control and data on separate ports is what every real client does, and
here it is not even a choice: peers speak a binary protocol over raw sockets and
have never heard of HTTP, so `/metrics` could not contend with a piece transfer
if it tried.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from .client import TorrentStatus
from .errors import BadRequest, NotFound
from .state import AppState, get_state
from .types import InfoHash

__all__ = ["MAX_TORRENT_BYTES", "router"]

logger = structlog.get_logger(__name__)

StateDep = Annotated[AppState, Depends(get_state)]

router = APIRouter()

MAX_TORRENT_BYTES = 4 * 1024 * 1024
"""Cap on an uploaded `.torrent` body.

Generous — a torrent for a 100 GiB file with 1 MiB pieces carries about 2 MiB of
piece hashes — and present because `await request.body()` buffers the whole
thing in memory. This is the same "bound before you allocate" rule the peer wire
is graded on, applied at the one place an untrusted body enters over HTTP.

`Content-Length` is a *claim* by the client, so it is checked first as a cheap
rejection and then the actual body length is checked again after reading. A
chunked upload sends no `Content-Length` at all, which is exactly why the second
check is not redundant. Streaming the body and aborting mid-read would be
stricter still, and is a reasonable thing to want once you have measured that
you need it."""


class MagnetBody(BaseModel):
    uri: str = Field(min_length=1)


class AddTorrentResponse(BaseModel):
    info_hash: InfoHash


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness only.

    Deliberately not a readiness check, and the distinction has teeth for a
    peer-to-peer client: this answers `ok` while every tracker is down, while no
    peer has unchoked you, while a download sits at 0%. Wiring an orchestrator to
    restart the process on any of those would take a client that is merely
    waiting for a swarm and turn it into one that never gets a chance to join
    — and a restart costs you a full re-hash of what is on disk on the way back,
    which makes the outage longer rather than shorter.

    If you want a readiness signal, build it from `GET /torrents` and say
    explicitly what "not ready" means.
    """
    return {"status": "ok"}


@router.get("/config")
async def config(state: StateDep) -> dict[str, Any]:
    """The tunables this process is running with.

    Worth having because every number in `docs/19-benchmarks.md` is meaningless
    without the configuration that produced it, and asking the running process
    beats trusting a `.env` you think you remember editing.

    Note what is absent: the peer id. See `Settings.public_status` — it is an
    allowlist, and the SPEC's security item is that this client's identity is not
    published next to anything that would let an observer correlate it.
    """
    return state.settings.public_status()


@router.get("/torrents")
async def list_torrents(state: StateDep) -> list[TorrentStatus]:
    """Every managed torrent and its progress."""
    return state.client.status()


@router.post("/torrents", status_code=status.HTTP_202_ACCEPTED)
async def add_torrent_file(request: Request, state: StateDep) -> AddTorrentResponse:
    """Add a torrent from a raw `.torrent` body.

    `202 Accepted` rather than `201`: adding kicks off announces and peer
    connections that will outlive this request by hours, so acknowledging the
    *intent* is the honest status. A `201 Created` would be claiming the download
    exists, and the client would reasonably then ask for it.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_TORRENT_BYTES:
        raise BadRequest(f"torrent exceeds {MAX_TORRENT_BYTES} bytes")

    raw = await request.body()
    if not raw:
        raise BadRequest("empty body — POST the .torrent file's bytes")
    if len(raw) > MAX_TORRENT_BYTES:
        raise BadRequest(f"torrent exceeds {MAX_TORRENT_BYTES} bytes")

    info_hash = await state.client.add_torrent_file(raw)
    return AddTorrentResponse(info_hash=info_hash)


@router.post("/torrents/magnet", status_code=status.HTTP_202_ACCEPTED)
async def add_magnet(body: MagnetBody, state: StateDep) -> AddTorrentResponse:
    """Add a torrent from a `magnet:` URI.

    The second entry point is a graded item, not a convenience: a magnet
    identifies content without describing it, so supporting both proves the
    engine can start from an infohash alone and learn the rest from peers.
    """
    info_hash = await state.client.add_magnet(body.uri)
    return AddTorrentResponse(info_hash=info_hash)


@router.get("/torrents/{info_hash}")
async def get_torrent(info_hash: str, state: StateDep) -> TorrentStatus:
    """One torrent's live progress and rates.

    The path parameter is the 40-character hex form — hex on the wire, raw bytes
    in the protocol, and the conversion happens exactly here so nothing
    downstream has to wonder which it holds.
    """
    parsed = InfoHash.from_hex(info_hash)
    if parsed is None:
        raise BadRequest("info_hash must be 40 hex characters")
    found = state.client.get(parsed)
    if found is None:
        raise NotFound()
    return found
