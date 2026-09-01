"""The engine that ties the verticals together — wiring, so the interesting
logic stays in the vertical modules it calls.

`Client` holds the run-wide identity and configuration (the peer id, the listen
port, the download directory, the caps), the process-wide disk thread pool, and
a registry of managed torrents. `add_torrent_file` / `add_magnet` parse a source
into an infohash (V2) and record it; the control plane reads the registry for
`GET /torrents`. Driving each torrent's tracker, peer and download tasks is the
TODO you fill in as V3-V6 come online.

## Why the thread pool lives here

Piece I/O is blocking (see `download.py`), so it runs in threads. The pool is
owned at *this* level rather than per torrent, and that is the "bounded pool
sized on purpose" checklist item made structural: one pool per process, sized
against `MAX_PEERS`, so adding a tenth torrent does not multiply your thread
count by ten. `asyncio.to_thread` would have been the easy alternative and it
shares the interpreter's default executor with every other library in the
process — which is exactly the pool nobody sized on purpose.

## The registry is a plain dict, and that is not an oversight

One process, one event loop, and every mutation happens in a coroutine. There is
no preemption between two `await` points, so a plain dict needs no lock — Rust
needed a `Mutex` here because its runtime is genuinely multi-threaded, and
porting that lock across would add contention to buy nothing. What *would* need
one is a mutation from inside `self._pool`; keeping the pool for file bytes
only, never for registry updates, is what keeps this true.
"""

from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor

import structlog
from pydantic import BaseModel

from .config import Settings
from .metainfo import MagnetLink, Metainfo
from .types import HASH_LEN, InfoHash, PeerId

__all__ = ["Client", "TorrentStatus", "generate_peer_id"]

logger = structlog.get_logger(__name__)

PEER_ID_PREFIX = b"-PB0001-"
"""Azureus-style client identifier: a dash, two letters, four version digits, a
dash. Trackers and peers use it for statistics and, occasionally, to refuse
clients known to misbehave. `PB` for this one — the two letters are unregistered
and picking a real client's is impolite, because the swarm will attribute your
bugs to them."""


class TorrentStatus(BaseModel):
    """A point-in-time view of a managed torrent — what `GET /torrents`
    renders."""

    info_hash: InfoHash
    name: str
    total_length: int
    downloaded: int = 0
    uploaded: int = 0
    peers: int = 0
    have_pieces: int = 0
    total_pieces: int = 0

    @property
    def progress(self) -> float:
        """Fraction complete by **piece count**, not by bytes.

        Pieces are the unit that is verified, so this cannot claim progress for
        bytes that arrived but failed their hash — which is the number a user
        actually wants and the one a naive byte counter gets wrong.
        """
        if self.total_pieces == 0:
            return 0.0
        return self.have_pieces / self.total_pieces


class Client:
    """The engine. One per process."""

    def __init__(self, settings: Settings, peer_id: PeerId | None = None) -> None:
        self.settings = settings
        self.peer_id = peer_id if peer_id is not None else generate_peer_id()
        self._torrents: dict[InfoHash, TorrentStatus] = {}
        self.pool = ThreadPoolExecutor(
            max_workers=settings.disk_workers,
            thread_name_prefix="piece-io",
        )
        """The process-wide disk pool. Handed to every `PieceStore` — see the
        module docstring."""

    async def add_torrent_file(self, raw: bytes) -> InfoHash:
        """Add a torrent from raw `.torrent` bytes, returning its infohash.

        Wired: it parses the source (V2) and records the torrent so it appears
        in `GET /torrents`. On the scaffold this raises `NotImplementedError`
        inside `Metainfo.from_bytes` the moment it is called — V2 is where the
        worklist starts, and `POST /torrents` is how you trip it.

        TODO(engine): once V2-V6 exist, start this torrent's tasks here —
        announce to its trackers (V3), dial and handshake peers (V4), open a
        `PieceStore` and run the piece loop (V5), and register it with the
        seeder (V6). Track live progress back into the `TorrentStatus`.
        """
        meta = Metainfo.from_bytes(raw)
        status = TorrentStatus(
            info_hash=meta.info_hash,
            name=meta.name,
            total_length=meta.total_length,
            total_pieces=meta.piece_count,
        )
        return self.register(status)

    async def add_magnet(self, uri: str) -> InfoHash:
        """Add a torrent from a `magnet:` URI, returning its infohash.

        A magnet has no metainfo yet — the piece table is fetched from peers
        later (BEP 9) — so the status starts with a zero length and no pieces,
        and the hex id stands in until a name is known. That is not a
        placeholder to tidy up: it is the honest state of a torrent you have
        identified but not yet described, and every consumer has to handle it.
        """
        magnet = MagnetLink.parse(uri)
        status = TorrentStatus(
            info_hash=magnet.info_hash,
            name=magnet.name or magnet.info_hash.hex(),
            total_length=0,
        )
        return self.register(status)

    def register(self, status: TorrentStatus) -> InfoHash:
        """Record a torrent, or return the existing one if we already have it.

        Adding the same torrent twice is a duplicate, not an error — the
        infohash *is* the identity, so a second `.torrent` for the same content
        names something already being downloaded. Replacing the entry would
        discard live progress; ignoring it keeps the operation idempotent, which
        is what a client retrying a `POST` deserves.
        """
        existing = self._torrents.get(status.info_hash)
        if existing is not None:
            logger.info("torrent already managed", info_hash=status.info_hash.hex())
            return existing.info_hash
        self._torrents[status.info_hash] = status
        logger.info("torrent added", info_hash=status.info_hash.hex(), name=status.name)
        return status.info_hash

    def status(self) -> list[TorrentStatus]:
        """Every managed torrent's current status."""
        return list(self._torrents.values())

    def get(self, info_hash: InfoHash) -> TorrentStatus | None:
        """One torrent's status, if managed."""
        return self._torrents.get(info_hash)

    async def close(self) -> None:
        """Release the disk pool.

        TODO(SPEC · ship it): this is also where a graceful shutdown announces
        `stopped` to every tracker (V3) and flushes any in-flight piece writes
        (V5), before the pool goes away. Ordering matters and reads backwards
        from the promise: nothing half-written may be left, so the pool is the
        *last* thing to shut down, after the announces that might still need it.

        `wait=True` blocks until in-flight file operations finish. That is a
        blocking call — deliberately, and safely, because by the time this runs
        the listeners are closed and there is nothing left to starve.
        """
        self.pool.shutdown(wait=True)
        logger.info("client closed", torrents=len(self._torrents))


def generate_peer_id() -> PeerId:
    """Mint this run's 20-byte peer id: a client prefix plus random bytes.

    Identity plumbing — the *protocol* is the learning, not this.

    `secrets.token_bytes` rather than `random.randbytes` because the peer id is
    the closest thing this client has to a per-run identifier, and a predictable
    one lets an observer correlate your sessions across trackers. That is the
    SPEC's "never logged raw alongside anything that would deanonymize a peer"
    item, addressed at the source rather than at the log line: `random` is seeded
    from the system clock and is not built to resist anyone guessing it, and
    `secrets` costs nothing here.
    """
    return PeerId(PEER_ID_PREFIX + secrets.token_bytes(HASH_LEN - len(PEER_ID_PREFIX)))
