"""V6 - The seeder: serving pieces fairly under load. `src/bittorrent/seeder.py`.

The upload half. Accept inbound peers, complete the handshake (V4), and answer
their `request` messages with `piece` data read from the verified store (V5).
The catch — and the whole reason a single seed can survive a swarm — is that
**you cannot upload to everyone at once**. Upload bandwidth is finite: fan out
to all N peers and every one of them crawls, your buffers fill with data nobody
is draining, and aggregate throughput collapses while every leecher sits at 3%.

So you run the **choke algorithm**. A small fixed number of **upload slots**
(regular unchokes, re-evaluated about every 10 seconds), plus one **optimistic
unchoke** (a random choked peer, rotated about every 30 seconds) so newcomers
can get a foot in the door and eventually earn a real slot. Everyone else waits,
choked, connected, and costing you almost nothing. Cap total connections, and
stream blocks from disk so per-peer memory never depends on the file's size.

That bounded, deliberate scheduling is what defeats the flash crowd — which is
this project's boss fight, and the only vertical whose failure mode is not an
error but a number that quietly goes the wrong way.

*Concept to internalize:* why finite upload bandwidth forces a scheduler, upload
slots plus optimistic unchoke as a fair bounded policy, and backpressure on the
upload path.

## `await writer.drain()` *is* the bounded-memory criterion

`writer.write(data)` on an asyncio stream does not send and does not block — it
appends to the transport's buffer and returns. Serve a 16 KiB block to a peer
that has stopped reading and the bytes sit in your process. Serve a thousand
without ever awaiting and you are holding megabytes for one slow peer, per peer,
and the boss fight's "RSS stays bounded" line fails with no error anywhere.

`await writer.drain()` is the backpressure point: it returns immediately while
the transport's buffer is under its high-water mark and suspends when it is not.
Draining after each block is what makes a slow peer slow *you serving that peer*
rather than growing your heap — which is the entire mechanism behind the
criterion, and one line.

## The three asyncio facts the wired parts below are built on

**One task per connection, so state needs no locks.** Everything about a peer
— its state flags, its buffer, whether it holds a slot — lives in the coroutine
serving it. The choke *decision* is the only shared thing, which is why it is a
pure function over a snapshot rather than something that reaches into sessions.

**Cancelling a task parked in `read()` is safe; cancelling one mid-send is
not.** The drain in `close()` leans on exactly that: stop accepting, cancel the
idle ones immediately, let the busy ones finish the block they are writing.

**`random.choice` needs a sequence.** The optimistic unchoke picks from the set
of choked peers, and `random.choice(some_set)` raises `TypeError` — `random`
dropped set support in 3.11. `random.choice(list(choked))` or
`random.sample(sorted(choked), 1)`, and the fact that you had to think about
determinism to write that line is a hint that the test will want to inject a
seeded `Random` rather than fight the global one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import structlog

from .config import Settings
from .tracker import PeerAddress

if TYPE_CHECKING:
    from .client import Client

__all__ = [
    "DRAIN_TIMEOUT",
    "OPTIMISTIC_INTERVAL",
    "UNCHOKE_INTERVAL",
    "PeerSession",
    "Seeder",
    "UnchokeCandidate",
    "choke_loop",
    "pick_optimistic",
    "select_unchoked",
]

logger = structlog.get_logger(__name__)

UNCHOKE_INTERVAL = 10.0
"""Seconds between regular unchoke rounds.

BitTorrent's own number, and not arbitrary: TCP needs a few seconds to find a
connection's throughput, so re-deciding faster than that means judging peers on
measurements that are mostly slow-start. Ten seconds is long enough to have
learned something and short enough that a peer who stops reciprocating loses the
slot while you still care."""

OPTIMISTIC_INTERVAL = 30.0
"""Seconds between optimistic-unchoke rotations - three unchoke rounds, so a
newcomer who turns out to be fast has time to prove it and be promoted to a
regular slot before the rotation takes the free one back."""

DRAIN_TIMEOUT = 5.0
"""Seconds to let in-flight peer sessions finish their current send on shutdown
before they are cancelled."""


@dataclass(frozen=True, slots=True)
class UnchokeCandidate:
    """One peer's inputs to the choke decision, as a snapshot.

    A value object rather than a live session reference on purpose: the choke
    decision becomes a **pure function over a list of these**, which is what
    makes the boss fight's central claim — never more than `K` unchoked — a unit
    test instead of a load test. A policy that reached into live sessions could
    only be checked by running a swarm at it.
    """

    peer: PeerAddress

    interested: bool
    """Whether they have said `interested`. Unchoking a peer who wants nothing
    burns a slot to no effect, which is the simplest way to fail the boss fight
    while holding the cap perfectly."""

    bytes_sent: int
    """What we have uploaded to them this round. A pure seed has no downloads to
    reciprocate, so this is the usual stand-in for "is this peer actually
    consuming what I give them"."""

    connected_at: float
    """`loop.time()` when they arrived — the input to any policy that wants to
    avoid starving newcomers, and the reason the optimistic unchoke exists at
    all."""


class PeerSession:
    """Per-connection state for one inbound peer. One per task, never shared."""

    __slots__ = ("busy", "peer", "unchoked")

    def __init__(self, peer: PeerAddress) -> None:
        self.peer = peer
        self.unchoked = False
        """Whether this peer currently holds an upload slot. The sum of these
        across sessions is what `bt_peers_unchoked` reports, and what the boss
        fight checks against `UPLOAD_SLOTS + 1`."""
        self.busy = False
        """True while a block is being read or written. False means the task is
        parked waiting for a message with nothing owed, which is what lets
        shutdown cancel it instantly instead of waiting out the drain budget."""


class Seeder:
    """The inbound peer listener and the choke scheduler.

    Constructed in the lifespan, started with `start()`, stopped with `close()`.
    The accept loop, the connection registry and the drain are **wired**; what a
    session *does* and who gets a slot are V6.
    """

    def __init__(self, client: Client, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self._server: asyncio.Server | None = None
        self._sessions: dict[asyncio.Task[None], PeerSession] = {}
        self._closing = False
        self.port = 0
        """The port actually bound. Not `settings.peer_port`, which may be 0
        meaning "any free port" — and this is the value that must be
        **announced**, since telling a tracker you are reachable on port 0 tells
        the swarm nothing can reach you."""

    @property
    def connection_count(self) -> int:
        return len(self._sessions)

    @property
    def unchoked_count(self) -> int:
        """Peers currently holding a slot — the gauge the boss fight watches."""
        return sum(1 for session in self._sessions.values() if session.unchoked)

    def candidates(self) -> list[UnchokeCandidate]:
        """A snapshot of the live sessions, for the choke decision.

        Built here rather than inside the policy so the policy stays a pure
        function — see `UnchokeCandidate`.

        TODO(V6): fill in `interested`, `bytes_sent` and `connected_at` from the
        session's real state once sessions track them. The empty-ish shape below
        is why `choke_loop` runs harmlessly on the scaffold.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        return [
            UnchokeCandidate(peer=s.peer, interested=False, bytes_sent=0, connected_at=now)
            for s in self._sessions.values()
        ]

    async def start(self, host: str = "0.0.0.0", port: int | None = None) -> int:
        """Bind and begin accepting inbound peers. Returns the bound port."""
        self._server = await asyncio.start_server(
            self._handle,
            host,
            port if port is not None else self.settings.peer_port,
        )
        self.port = int(self._server.sockets[0].getsockname()[1])
        logger.info("seeder listening for inbound peers", port=self.port)
        return self.port

    async def close(self) -> None:
        """Stop accepting, let in-flight sends finish, then cancel the rest.

        The three steps are the V6 shutdown criterion in order. Announcing
        `stopped` to the trackers is deliberately *not* here — it belongs to the
        client, and it has to happen after this returns, because a `stopped`
        announced while you are still serving is a lie you then spend five
        seconds making true.
        """
        self._closing = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

        if not self._sessions:
            return

        # Idle sessions are cancelled now: parked waiting for a message, owing
        # nothing. Waiting on them would mean spending the whole drain budget on
        # peers doing nothing, which is how `docker stop` turns into a five
        # second hang with one idle peer attached.
        #
        # Reading every flag and then cancelling needs no lock, and that is not
        # luck: there is no `await` between the check and the cancel, so no
        # session can go from idle to busy in the middle of this loop. Add an
        # `await` in here and that stops being true.
        idle = [task for task, session in self._sessions.items() if not session.busy]
        for task in idle:
            task.cancel()

        done, pending = await asyncio.wait(self._sessions, timeout=DRAIN_TIMEOUT)
        if pending:
            logger.warning("peer sessions did not finish in time", count=len(pending))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info(
            "peer sessions drained",
            finished=len(done),
            idle_cancelled=len(idle),
            timed_out=len(pending),
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one inbound peer until it disconnects or the seeder drains.

        TODO(security · connection cap): bound concurrent peers at
        `settings.max_peers` — an `asyncio.Semaphore` acquired here, or a check
        against `connection_count` that closes the connection immediately.
        Without it a connection flood exhausts file descriptors, and the failure
        mode is that the process stops accepting *everything*, including
        whatever you would have used to diagnose it.

        TODO(observability): bump `metrics.PEERS_CONNECTED` here and decrement it
        in the `finally` — in the `finally` specifically, because a session that
        dies from an exception still has to stop being counted, and a gauge that
        only goes up is worse than no gauge.
        """
        peer = _peer_address(writer)
        session = PeerSession(peer)
        task = asyncio.current_task()
        if task is not None:
            self._sessions[task] = session
        try:
            await serve_peer(self, session, reader, writer)
        except (ConnectionResetError, BrokenPipeError):
            # The peer went away mid-write. Entirely normal — under the boss
            # fight's flood it is how most sessions end.
            logger.debug("peer connection reset", peer=peer)
        except asyncio.CancelledError:
            raise
        except NotImplementedError as exc:
            # The scaffold's expected state: V6 (or something it leans on) is
            # unbuilt. Logged at info so the first inbound peer names the
            # vertical rather than looking like a crash.
            logger.info("peer session hit an unbuilt vertical", peer=peer, detail=str(exc))
        except Exception as exc:
            logger.warning(
                "peer session failed", peer=peer, error=str(exc), kind=type(exc).__name__
            )
        finally:
            if task is not None:
                self._sessions.pop(task, None)
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()


async def serve_peer(
    seeder: Seeder,
    session: PeerSession,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Serve one connected peer for the life of the connection.

    TODO(V6): read their handshake (`peer.read_handshake`), check the infohash
    is one **we are actually managing** and drop them if not, send ours back, and
    send our `bitfield` (`peer.pack_bitfield` over the store's `have`). Then
    loop on `peer.read_message`:

    * `Interested` / `NotInterested` -> record it on the session; the choke loop
      reads it next round.
    * `Request` -> if `session.unchoked`, read the block with
      `PieceStore.read_block` and write a `Piece` back, then **`await
      writer.drain()`** (see the module docstring — this is the bounded-memory
      criterion). If they are choked, ignore it: that is what choked means, and
      answering anyway makes the slot cap decorative.
    * anything else -> ignore, including `Unknown`. Forward compatibility is a
      horizontal item and this is where it is either honoured or not.

    Set `session.busy = True` around the read-and-send and back to `False` when
    parked, or shutdown cannot tell a peer mid-block from an idle one.

    A request for a piece we do not have, or one that is out of range or
    oversized, is **refused** — `read_block` raises `PeerError`, and the right
    response is to log it and drop that connection, never to serve something and
    never to crash. A peer flooding `request`s is the flood test: bounded because
    you answer them one at a time from a bounded store, not because you counted
    them.
    """
    raise NotImplementedError("V6: seed to a peer - handshake, bitfield, then serve requests")


def select_unchoked(candidates: Sequence[UnchokeCandidate], slots: int) -> set[PeerAddress]:
    """Choose which peers hold the **regular** upload slots this round.

    Returns at most `slots` peers. The optimistic unchoke is a separate decision
    (`pick_optimistic`) so that this function's postcondition is exactly the
    thing the boss fight checks — `len(result) <= slots`, with no "+1" to argue
    about — and so the two policies can be tested and changed independently.

    TODO(V6): pick the peers. A pure seed has no downloads to reciprocate, so
    the classic tit-for-tat ranking does not apply directly and you have a real
    choice to make:

    * **fastest first** — rank by `bytes_sent` over the last round, which favours
      peers who actually consume what you give them and is closest to what real
      clients do;
    * **round-robin** — rotate slots by `connected_at`, which is fairest and
      slowest;
    * **random** — trivial, surprisingly hard to beat, and a genuinely useful
      baseline to measure the others against.

    Filter to `interested` candidates first; a slot given to a peer who wants
    nothing is a slot that transfers nothing.

    Whichever you choose, `docs/19-design.md` has to name it and say why — the
    choke policy is one of the three decisions the Definition of done grades.
    """
    raise NotImplementedError("V6: choose at most `slots` regular unchokes")


def pick_optimistic(
    candidates: Sequence[UnchokeCandidate],
    regular: set[PeerAddress],
) -> PeerAddress | None:
    """Choose one **choked** peer to unchoke speculatively. `None` if there is
    nobody to promote.

    This is the swarm's bootstrap mechanism and it is easy to dismiss as noise.
    Without it a new peer with nothing to offer is never given anything, so it
    never has anything to offer — a deadlock the whole swarm pays for, and the
    reason the boss fight measures **time-to-first-block for a newly arriving
    leecher** rather than just aggregate throughput.

    TODO(V6): pick uniformly at random from the candidates *not* in `regular`.
    `random.choice` needs a sequence, so `random.choice(list(...))` — and take a
    `random.Random` as an argument if you want the test to be able to seed it,
    which is much easier than asserting on a distribution.

    Worth knowing: real clients weight newly-connected peers about three times
    more likely, precisely so a newcomer's first chance arrives sooner than
    uniform selection would give it.
    """
    raise NotImplementedError("V6: pick one random choked peer as the optimistic unchoke")


async def choke_loop(seeder: Seeder, settings: Settings) -> None:
    """Re-run the choke decision on a timer until cancelled. Wired.

    The rhythm is the protocol's: regular slots every `UNCHOKE_INTERVAL`, the
    optimistic slot rotating every `OPTIMISTIC_INTERVAL` — three regular rounds,
    so a promising newcomer has time to prove itself before the rotation moves
    on.

    Tolerating `NotImplementedError` here is deliberate, and the same shape every
    background loop in this repo uses: on the scaffold the policy raises every
    tick, and a loop that died on the first one would look exactly like a loop
    that was working — right up until the boss fight, where the unchoke set
    silently never changes.
    """
    rounds = 0
    while True:
        try:
            await asyncio.sleep(UNCHOKE_INTERVAL)
            rounds += 1
            candidates = seeder.candidates()
            regular = select_unchoked(candidates, settings.upload_slots)
            unchoked = set(regular)

            if rounds % int(OPTIMISTIC_INTERVAL / UNCHOKE_INTERVAL) == 0:
                optimistic = pick_optimistic(candidates, regular)
                if optimistic is not None:
                    unchoked.add(optimistic)

            # TODO(V6): apply the decision — send `unchoke` to peers that just
            # gained a slot and `choke` to peers that just lost one, and update
            # `session.unchoked` so `unchoked_count` (and the metric built on it)
            # tells the truth. Sending nothing here means the policy is computed
            # and thrown away, which is the failure the boss fight sees as "the
            # seeder never unchoked anybody".
            logger.debug("choke round", unchoked=len(unchoked), candidates=len(candidates))
        except asyncio.CancelledError:
            raise
        except NotImplementedError:
            logger.debug("choke policy is unbuilt (V6)")
        except Exception as exc:
            logger.warning("choke round failed", error=str(exc), kind=type(exc).__name__)


def _peer_address(writer: asyncio.StreamWriter) -> PeerAddress:
    """The peer's `(host, port)`, for both logging and the choke decision.

    An address rather than a formatted string, because it is an *identity*: the
    choke policy keys on it, and a display string would have to be parsed back
    into one at exactly the moment it matters.

    `get_extra_info` is untyped because what it returns depends on the transport
    — an IPv4 peer is a 2-tuple, IPv6 a 4-tuple, a Unix socket a string — so the
    cast is where that unknown stops. It is a claim about asyncio's transports,
    not a silenced error, and the fallback covers the transports that have no
    address to give.
    """
    peer = cast("tuple[object, ...] | str | None", writer.get_extra_info("peername"))
    if isinstance(peer, tuple) and len(peer) >= 2:
        host, port = peer[0], peer[1]
        return str(host), port if isinstance(port, int) else 0
    return (str(peer) if peer else "unknown"), 0
