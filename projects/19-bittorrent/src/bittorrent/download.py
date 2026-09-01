"""V5 - Piece selection & verification: assembling a file from strangers.
`src/bittorrent/download.py`.

This is the leech loop. You have peers (V3), a wire to talk to them (V4), and a
table of piece hashes (V2). Now: decide *which* piece to fetch, split it into
blocks of at most 16 KiB and pipeline the `request`s across the peers that have
it, reassemble, and — the crux — **verify the piece's SHA-1 against the
metainfo hash before it counts as yours**. You are building a file out of bytes
handed to you by anonymous strangers. Verify-before-write is the trust boundary,
and a piece that fails is discarded and refetched from someone else.

Two scheduling ideas separate a healthy swarm from a naive one:

* **Rarest-first** - fetch the piece the *fewest* peers have, read off their
  bitfields. No piece goes extinct, the scarce blocks spread fastest, and you
  stay useful to others. Sequential download starves the swarm and, through it,
  starves you.
* **Endgame** - for the last few missing blocks, ask *several* peers at once and
  `cancel` the losers. It trades a little duplicate bandwidth for not stalling
  at 99% behind one peer who has gone quiet.

*Concept to internalize:* why rarest-first keeps a swarm alive, verify-before-
write as the trust boundary, and endgame as a latency/bandwidth trade.

## Where the blocking work goes - the decision this module forces

Every other layer in this project is already async: sockets are asyncio's,
tracker announces are asyncio's. Piece I/O is not. `os.pread` and `os.pwrite`
are synchronous syscalls, and CPython has no async filesystem API that is not a
thread pool wearing a costume. So the checklist's "no blocking call on the event
loop" stops being hygiene here and becomes the central design question.

Stated honestly:

* **A block read** is tens of microseconds warm from the page cache, and
  milliseconds cold. The GIL is released for the syscall, so a thread pool
  genuinely overlaps these — this is the case threads are actually good at.
* **A piece SHA-1** is 256 KiB through `hashlib`, which also releases the GIL
  for buffers of any real size. It parallelizes too.
* **The piece picker and the framing** are Python bytecode. They hold the GIL,
  and no pool makes that disappear — only doing less of it does.

Which is why `PieceStore` takes a **bounded** `ThreadPoolExecutor` from the
`Client` rather than making its own. One pool for the process, sized against
`MAX_PEERS`, is the "bounded pool sized on purpose" item; a pool per torrent
would multiply threads by torrents and an unbounded `asyncio.to_thread` shares
the default executor with everything else in the process. `PYTHONASYNCIODEBUG=1`
will tell you when you got it wrong — it logs any callback that occupies the
loop for over 100 ms — and the reasoning belongs in `docs/19-design.md`.

## `os.pread` / `os.pwrite`, and why not `seek` + `read`

A file object has **one shared file position**. Two threads doing `f.seek(off)`
then `f.read(n)` on the same object will interleave between the two calls and
read from each other's offsets — a data race that produces *plausible wrong
bytes*, so it fails the SHA-1 check and looks exactly like a lying peer. Days
get lost there.

`os.pread(fd, n, offset)` and `os.pwrite(fd, data, offset)` take the offset as
an argument and never touch the shared position, which makes them safe to call
from many threads against one descriptor. That is the entire reason to prefer
them, and it is worth knowing before you need it.

## Two hashing notes

`hashlib.sha1(data, usedforsecurity=False)` keeps working on FIPS-enabled
builds, where the plain call raises. And comparing digests here is a plain `==`:
`hmac.compare_digest` defends against a *timing* attack, which requires a secret
to leak, and a piece hash is published in the torrent for anyone to read.
Reaching for constant-time comparison here is cargo-culting — the place it
genuinely belongs is a password check, not this.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import structlog

from .metainfo import Metainfo
from .peer import BLOCK_SIZE

__all__ = ["BLOCK_SIZE", "PieceStore", "pick_piece", "verify_piece"]

logger = structlog.get_logger(__name__)


class PieceStore:
    """The on-disk backing store for one torrent, plus the have-set.

    Both halves of the project meet here: the download loop writes verified
    pieces in, and the seeder (V6) reads blocks back out. So this class owns the
    file layout and the single authoritative answer to "do I have piece `i`?" —
    and `have` means *downloaded and verified*, never *received*. Collapsing
    those two meanings is how a corrupt piece gets announced to the swarm with a
    `have` and then served to somebody else.
    """

    __slots__ = ("_meta", "_pool", "_root", "have")

    def __init__(self, meta: Metainfo, root: Path, pool: ThreadPoolExecutor) -> None:
        """Wired, so the scaffold can construct a store and the app can boot.

        `pool` is the process-wide bounded executor, handed down from the
        `Client` — see the module docstring on why it is not made here.
        """
        self._meta = meta
        self._root = root
        self._pool = pool
        self.have: set[int] = set()
        """Indices of pieces present on disk **and verified**. Empty until
        `scan_existing` runs."""
        root.mkdir(parents=True, exist_ok=True)

    @property
    def meta(self) -> Metainfo:
        return self._meta

    @property
    def root(self) -> Path:
        return self._root

    def has_piece(self, index: int) -> bool:
        return index in self.have

    @property
    def have_count(self) -> int:
        return len(self.have)

    @property
    def is_complete(self) -> bool:
        return len(self.have) == self._meta.piece_count

    @property
    def missing(self) -> set[int]:
        """The pieces still wanted — the input to `pick_piece`."""
        return set(range(self._meta.piece_count)) - self.have

    async def scan_existing(self) -> None:
        """Lay out the files and work out what is already on disk.

        TODO(V5): create (or open) each file under `root`, pre-size it —
        `f.truncate(length)` gives you a sparse file, so a 4 GiB torrent costs no
        disk until pieces actually land — and then populate `have` by **re-
        hashing what is there**. On a fresh directory that finds nothing; on a
        restart it is the resume criterion, and it is the only honest way to
        implement resume: a sidecar file claiming which pieces are done is a
        claim, and re-hashing is a fact.

        Build every path through `metainfo.safe_relative_path`. It is the one
        place a hostile torrent gets to influence what you write, and skipping it
        for the single-file case is how the guard ends up half-applied.

        Two properties this method should have, worth designing for rather than
        retrofitting. Rehashing a large torrent is minutes of `hashlib` — run it
        on `self._pool`, not on the loop, or startup blocks every other torrent.
        And it must be safe to run over a directory containing a partially
        written piece from a `kill -9`: a piece that fails its hash is simply not
        in `have`, which is exactly the right outcome and needs no special case.
        """
        raise NotImplementedError("V5: allocate the files and rebuild `have` by re-hashing")

    async def read_block(self, index: int, begin: int, length: int) -> bytes:
        """Read one block, for the seeder to serve (V6).

        TODO(V5): bounds-check first, then read. The checks are the security
        criterion, and all three are `PeerError`: we must actually have
        `index`, `begin + length` must not run past that piece's true size (see
        `Metainfo.piece_size` — the last piece is short), and `length` must not
        exceed `BLOCK_SIZE`. A peer asking for a 4 GiB "block" gets refused, not
        served and not crashed into.

        Then map `(index, begin)` to a byte offset — `index * piece_length +
        begin` — and read it. For a multi-file torrent that offset can **span a
        file boundary**, so the read may be two `os.pread`s stitched together;
        that arithmetic is the fiddly part of the vertical and it is worth a
        table-driven test more than a careful re-read.

        Run the syscall on `self._pool` and return the bytes. Streaming from disk
        per request is what keeps per-peer memory bounded — the criterion says no
        whole-file-per-peer buffering, and holding a piece cache per connection
        is exactly the thing the boss fight punishes.
        """
        raise NotImplementedError("V5: read a bounds-checked block for the seeder")

    async def write_verified_piece(self, index: int, data: bytes) -> None:
        """Persist a piece that has **already passed** `verify_piece`, and mark
        it have.

        TODO(V5): write `data` at the piece's offset — spanning file boundaries
        for a multi-file torrent, the same arithmetic as `read_block` and worth
        sharing with it — then add `index` to `have`.

        This method trusts its input completely, which is safe only because
        exactly one caller exists and it verifies first. Say so at that call
        site: the invariant lives in the caller, and a second caller added later
        without it is the bug this docstring is trying to prevent.

        `os.pwrite` on `self._pool`, for the reason in the module docstring.
        Whether `have` is updated before or after the write matters under a
        crash: a `have` announced for a piece not yet on disk is a promise to the
        swarm you cannot keep.
        """
        raise NotImplementedError("V5: write a verified piece and mark it have")

    async def close(self) -> None:
        """Release file handles. Wired.

        The executor is deliberately *not* shut down here — it belongs to the
        `Client` and is shared with every other torrent, so a store that closed
        it would break its siblings.
        """
        logger.debug("piece store closed", root=str(self._root), have=len(self.have))


def verify_piece(expected: bytes, data: bytes) -> bool:
    """Does `data` hash to the expected piece SHA-1?

    This one function is the trust boundary of the entire project. Everything
    upstream of it — the tracker's peer list, the peer's handshake, every block
    that arrived — is unverified input from a stranger. Everything downstream is
    bytes you have proven are the ones the torrent named.

    TODO(V5): `hashlib.sha1(data, usedforsecurity=False).digest() == expected`.
    A plain `==`, for the reason in the module docstring, and a comparison of the
    **full** digest — truncating to "the first 8 bytes are enough" is exactly the
    shortcut that makes forgery cheap.

    Return `False` rather than raising: the caller's response is to discard the
    piece and refetch it, which is ordinary control flow in a swarm full of
    unreliable peers, not an exceptional condition. Count the failure in
    `metrics.PIECES_VERIFIED_TOTAL` with `result="failed"` — a rising failure
    rate is how you *see* a lying peer, and it is the metric that turns "my
    download is slow" into "peer X has sent me forty bad blocks".
    """
    raise NotImplementedError("V5: SHA-1 the piece and compare to the metainfo hash")


def pick_piece(needed: AbstractSet[int], availability: Counter[int]) -> int | None:
    """Choose the next piece to download, rarest-first. `None` when done.

    `availability` counts how many connected peers advertise each piece —
    a `Counter` because that is what the stdlib gives you for exactly this
    (`counter.update(peer.has)` per peer, `subtract` when one disconnects), and
    because a missing key reads as `0` instead of raising, which is the correct
    answer for a piece nobody has.

    TODO(V5): among `needed`, return one with the lowest availability, breaking
    ties **randomly**.

    The tie-break is not a detail. `min(needed, key=...)` is deterministic, and
    on a fresh swarm where every piece has the same availability that means every
    client picks piece 0, then piece 1 — the entire swarm downloading in lockstep
    from the one seed, which is the pathology rarest-first exists to prevent.
    Collect the tied group and `random.choice` it.

    Two cases worth deciding rather than discovering: a piece with availability
    `0` is one **nobody has**, and returning it means requesting a piece that
    cannot arrive — filtering those out is usually right. And strict
    rarest-first is a bad *opening*: clients pick the first few pieces at random
    precisely so they have something to trade before they know what is rare.
    `docs/19-design.md` is where the strategy you chose gets named, and that is
    a graded line in the Definition of done.
    """
    raise NotImplementedError("V5: rarest-first selection with a random tie-break")


async def run_download(store: PieceStore) -> None:
    """Drive one torrent's download to completion.

    TODO(V5): the loop that ties every vertical together.

    Maintain per-piece `availability` from the peers' bitfields and `have`
    messages (V4). Pick a piece (`pick_piece`), split it into blocks of
    `BLOCK_SIZE`, and keep `pipeline_depth` requests in flight **per peer** —
    with a depth of 1 your ceiling is `BLOCK_SIZE / RTT` regardless of
    bandwidth, and that is the pipelining criterion in one sentence. Reassemble
    into a `bytearray(piece_size)` (assigning into slices of one buffer, rather
    than concatenating a growing `bytes`), `verify_piece` it,
    `write_verified_piece`, and broadcast `have` to every connected peer.

    For the tail, switch to **endgame**: request the last outstanding blocks from
    several peers at once and `cancel` the rest as they land. Without it a single
    peer that goes quiet at 99% stalls the whole download indefinitely, which is
    the most common way a from-scratch client "almost works".

    Return once every piece is verified.

    Structuring this is the real work and asyncio gives you the tools directly:
    a task per peer session, an `asyncio.Queue` of block requests, and
    `asyncio.TaskGroup` so that one peer raising cancels the group cleanly
    instead of leaving orphans behind. Watch three things that are easy to get
    wrong and hard to see:

    * **A `create_task` result nobody holds can be garbage-collected
      mid-flight.** Keep a reference to every peer task, or the download quietly
      loses peers with no error anywhere.
    * **A peer that stops sending must not stall the piece.** Give every
      in-flight block a deadline and re-queue it elsewhere; `asyncio.timeout` is
      the tool, and no timeout is the bug.
    * **The choke state decides everything.** Requests to a peer that is choking
      you are discarded silently by them — a loop that ignores `peer_choking`
      looks busy and transfers nothing.
    """
    raise NotImplementedError("V5: drive the leech loop (pick -> pipeline -> verify -> write)")
