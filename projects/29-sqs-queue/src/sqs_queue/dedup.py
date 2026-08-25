"""V5 — The deduplication window: "exactly-once" with a receipt and an expiry date.

A producer calls `SendMessage`. The response times out. Did the message land? The
producer cannot tell, so it sends again — and now the queue has two copies of an
order, an email, a charge. This is not an edge case: it is what every network
does under load, and it is the duplicate that is actually worth eliminating,
because the producer knows the two sends are the same and can say so.

So: a message carrying a `MessageDeduplicationId` that was already accepted
**within the window** is acknowledged with the *original* message id and not
enqueued. The producer sees success either way and stops retrying. That is the
whole feature.

**Now the part that matters more than the code: how narrow this claim is.**

* It removes **producer** duplicates. It does nothing about consumer-side
  duplicates — V1's visibility timeout will still deliver a message twice to a
  worker that was too slow, and no dedup id anywhere changes that. Consumers must
  still be idempotent. Every "exactly-once" in this industry is this shape:
  bounded dedup inside one system's boundary, sold with a word that means
  something much stronger.
* It is **five minutes**, not forever. The window is sized to cover a retrying
  client, and a duplicate that arrives at minute six is a new message. A caller
  who reads "exactly-once" and builds a payment on a daily reconciliation job has
  built something that will double-charge someone, and they will be surprised.
  Writing that sentence down in the design doc is a graded criterion.

**The engineering problem is bounded memory.** The window is a set that every
send consults and every send may add to, and it must not grow without bound: its
size is proportional to *window length × send rate*, never to total messages ever
sent. Entries expire — through V2's engine, not a scan — and past
`max_dedup_entries` the send path needs a defined behaviour rather than an
allocation it cannot make. Note the security angle too: if dedup ids are
attacker-chosen, then an attacker who can predict yours can *suppress* your
messages by sending them first, and one who sends a million distinct ones can
fill the window. Both are worth a sentence in the design doc.

Content-based dedup derives the id from the message body when the client does not
supply one — deterministic, so the same body yields the same id. Think about what
"the same body" should mean: bytes only, or body plus message attributes? SQS
hashes both, and the reason is that two messages differing only in an attribute
are two different messages to the consumer that reads it.

Scaffold state: the shapes are modelled; derivation, lookup and expiry raise.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .config import Settings
from .models import Message

__all__ = ["DedupResult", "DedupWindow"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DedupResult:
    """What the window says about a send.

    `original_message_id` is the point: a suppressed duplicate is not an error
    and not a silent drop — it is a success carrying the id of the message that
    did land, so the producer's bookkeeping stays correct without it ever
    learning that a duplicate happened.
    """

    duplicate: bool
    original_message_id: str | None = None
    original_sequence_number: int | None = None


class DedupWindow:
    """The time-bounded set of dedup ids, per queue.

    Consulted on every send to a FIFO queue, so its lookup is on the hot path of
    the boss fight's drain scenario. Its *memory* is the criterion, though: the
    SPEC measures it across a long run and asks that it track the window, not the
    run.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def derive_id(self, message: Message) -> str:
        """Content-based dedup: the id implied by the message itself.

        Deterministic by definition — same content, same id, on any node, at any
        time. Which means the hash inputs are a contract: change what goes into
        it and every in-flight producer's retries stop deduplicating, silently,
        for one window.

        Watch the event loop. Hashing a 256 KB body is real work, and doing it
        inline on every send is one of the two places in this service most likely
        to trip the Python-and-runtime checklist's slow-callback criterion.
        """
        # TODO(V5): derive a stable id from the message content. Decide — and
        # document — whether message attributes participate.
        raise NotImplementedError("V5: derive a content-based deduplication id")

    def check(self, queue_name: str, dedup_id: str, now: float) -> DedupResult:
        """Has this id been seen in this queue's window?

        Read-only: it answers the question and does not claim the id. Splitting
        check from claim looks like a courtesy and is actually the interesting
        part — between the two, a concurrent send with the same id can slip
        through, and the SPEC's "exactly one enqueue" criterion is asserted with
        exactly that concurrency. Decide whether these stay two calls.
        """
        # TODO(V5): look the id up in the window.
        raise NotImplementedError("V5: check a dedup id against the window")

    def claim(self, queue_name: str, dedup_id: str, message: Message, now: float) -> None:
        """Record an accepted message's dedup id, starting its window.

        `max_dedup_entries` is a real limit and the behaviour at it is a real
        decision: refuse the send (`OverLimit` — correct, and a caller-visible
        outage), or evict the oldest entry (available, and quietly stops
        deduplicating for whoever got evicted). One of them fails loudly and one
        fails silently. Pick, and say why in the design doc.
        """
        # TODO(V5): record the id and schedule its expiry through the deadline
        # engine — the window must not be swept by a scan.
        raise NotImplementedError("V5: claim a dedup id for the window")

    def expire(self, queue_name: str, dedup_id: str, now: float) -> bool:
        """Drop an id whose window has closed. Called by V2's engine.

        Returns whether anything was removed, so a high no-op rate shows up as
        the residue signal V2 asks for.
        """
        # TODO(V5): remove the entry if it is still the one that was scheduled.
        raise NotImplementedError("V5: expire a dedup id from the window")

    def size(self, queue_name: str | None = None) -> int:
        """How many ids are live. Plumbing for the gauge and the memory criterion."""
        # TODO(V5): report the window's size.
        raise NotImplementedError("V5: report the dedup window size")
