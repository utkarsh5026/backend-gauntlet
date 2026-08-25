"""V1 — Receipt handles: a lease you can only release with the token you were handed.

The bug this module exists to prevent, in full:

    worker A: ReceiveMessage  -> M, starts a 40s job
    (30s visibility timeout expires; M becomes available again)
    worker B: ReceiveMessage  -> M, starts the same job
    worker A: DeleteMessage(M) -> deletes B's delivery
    worker B: finishes, deletes nothing; its lease expires; M is delivered again

Nothing in that trace looks like an error. A is honest, B is honest, and the
queue quietly turned at-least-once into at-least-forever. The cause is that
`DeleteMessage(message_id)` addresses the *message* when the thing being released
is a *delivery* — and the two stopped being the same object the moment the first
lease expired.

So a receive does not hand back an id. It hands back a **receipt handle**: a
token minted for one delivery, presented to delete or extend that delivery, and
worthless the moment a later delivery supersedes it. `Message.generation` is what
makes "superseded" a comparison rather than a bookkeeping exercise.

Two design questions the SPEC grades, and neither has one obviously right answer:

* **What goes in the handle.** It must be unforgeable — a client that has never
  received a message must not be able to construct a handle for it — and it must
  be checkable. Those pull in different directions: everything you put *in* it
  can be checked without a lookup, and everything you put in it is also something
  a client can see. Real SQS handles are long, opaque, and different every time.
* **What a stale handle deserves.** A superseded handle is an honest slow worker,
  not an attack. Refusing it silently, refusing it loudly, and refusing it with a
  distinct error code are three different debugging experiences for whoever owns
  that consumer at 3am — and the observability checklist counts stale and
  malformed separately precisely because they mean opposite things.

Scaffold state: the shapes are modelled; minting, parsing, and every state
transition raise.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

import structlog

from .config import Settings
from .models import Delivery, Message

__all__ = ["DeleteOutcome", "InflightTable", "ReceiptHandleCodec"]

log = structlog.get_logger(__name__)


class DeleteOutcome(enum.StrEnum):
    """Why a delete did what it did.

    Returned rather than reduced to a bool because the three cases are three
    different metrics and, for a caller, three different follow-up actions. A
    consumer that gets `SUPERSEDED` has just learned its visibility timeout is
    too short — but only if you tell it apart from `DELETED`.
    """

    DELETED = "deleted"
    # The message was already gone. Idempotent, not an error: a retried delete
    # after a successful one is the single most common thing a queue client does.
    ALREADY_DELETED = "already_deleted"
    # A valid handle from an older delivery. The live delivery is untouched —
    # this is the case the whole module exists for.
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ParsedHandle:
    """A receipt handle, taken apart.

    Frozen because a handle is evidence, and evidence that a caller downstream
    can edit is not evidence.
    """

    message_id: str
    generation: int
    queue_name: str
    # When the handle itself stops being worth checking. The stretch goal: an
    # obviously-dead handle can be rejected from this field alone, without ever
    # touching the message table.
    expires_at: float


class ReceiptHandleCodec:
    """Mints and parses receipt handles.

    The unforgeability requirement lands here. Think about what a client could do
    with a handle it *did* receive — for a different message, on a different
    queue, an hour ago — and make sure each of those is refused. A codec that
    round-trips its own output correctly and accepts a neighbouring message's
    handle has passed every test you thought to write and none of the ones that
    matter.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def mint(self, message: Message, delivery_generation: int) -> str:
        """Issue a handle for one delivery of one message.

        Called once per delivery, on the hot path — so whatever this costs, it
        costs on every receive of every message. The boss fight's drain scenario
        will find an expensive implementation immediately.
        """
        # TODO(V1): mint an unforgeable, single-delivery token.
        raise NotImplementedError("V1: mint a receipt handle")

    def parse(self, handle: str) -> ParsedHandle:
        """Take a handle apart, or refuse it.

        Refusing is the important half. `ReceiptHandleIsInvalid` is for something
        you did not mint; a handle you *did* mint that names an older delivery is
        not this function's problem — it parses fine, and `InflightTable` decides
        what it is worth.
        """
        # TODO(V1): parse and authenticate the handle. Anything you did not mint
        # is `ReceiptHandleIsInvalid`; do not let a parse failure become a
        # generic 500, and do not let the error message reveal which check failed.
        raise NotImplementedError("V1: parse and authenticate a receipt handle")


class InflightTable:
    """The message lifecycle: available → in-flight → deleted, or back again.

    Holds the messages for one node and answers the four data-plane verbs. It
    does **not** own the clock — every deadline it needs (visibility expiry,
    delay, retention) is registered with V2's engine, which calls back into
    `expire_visibility` when one comes due. Keeping the clock out of here is what
    stops this class from growing a scan loop.
    """

    def __init__(self, settings: Settings, codec: ReceiptHandleCodec) -> None:
        self._settings = settings
        self._codec = codec

    def receive(self, messages: list[Message], now: float, visibility: float) -> list[Delivery]:
        """Take a batch of available messages in flight and mint their handles.

        Given the messages to hand out (the caller has already selected them —
        FIFO group rules are V4's job, not this one's), this performs the
        transition: bump the generation, count the receive, set the state, stamp
        the lease, mint a handle.

        The ordering inside here is where the race lives. Between "I decided this
        message is available" and "it is now in flight", another receive must not
        be able to see it. Make yourself say out loud what makes that true.
        """
        # TODO(V1): transition each message to in-flight and return its delivery.
        # TODO(V2): register each lease's expiry with the deadline engine — and
        # register it against the *generation*, so a deadline for a delivery that
        # has already ended is dropped rather than acted on.
        raise NotImplementedError("V1: transition messages to in-flight")

    def delete(self, handle: str, now: float) -> DeleteOutcome:
        """Release a delivery permanently — if the handle still names it.

        The three outcomes are the whole test suite for this module. Work out for
        each one what must be true of the message afterwards, and note that
        exactly one of them must leave a live delivery running.
        """
        # TODO(V1): parse the handle, compare its generation to the message's,
        # and act only when it names the current delivery.
        raise NotImplementedError("V1: delete a message by receipt handle")

    def change_visibility(self, handle: str, timeout: float, now: float) -> None:
        """Extend or shorten the current lease.

        This is how a consumer expresses "I need longer" — and, at `timeout=0`,
        "take it back now", which is the closest thing SQS has to a nack. Note
        what a zero must do to outstanding handles: the delivery is over, so the
        handle that asked for it is over too.

        Watch the interaction with V2. Moving a deadline that is already
        registered must not leave the old one behind to fire later; the generation
        check is what makes a leftover harmless, but leftovers that accumulate are
        the unbounded-residue failure V2 grades.
        """
        # TODO(V1): validate the handle names the current delivery, then move the
        # lease. Enforce the 12-hour ceiling here, not at delivery time.
        raise NotImplementedError("V1: change a message's visibility timeout")

    def expire_visibility(self, message_id: str, generation: int, now: float) -> bool:
        """A lease ran out — put the message back. Called by V2's engine.

        `generation` is the delivery the deadline was scheduled for. If the
        message has moved on since, this deadline is stale and must do nothing:
        that single check is what makes the timer-versus-delete race decidable
        instead of a coin flip.

        Returns whether the message actually became available again, so the
        caller can count expiries — a non-zero expiry rate means consumers are
        dying or the timeout is too short, and that number is on the dashboard.
        """
        # TODO(V1/V2): drop the deadline if the generation is stale; otherwise
        # return the message to available and bump the generation so the old
        # handle can never work again.
        raise NotImplementedError("V1: return an expired in-flight message to available")
