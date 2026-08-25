"""V6 — The control plane: attributes as a contract, redrive as the release valve.

Creating a queue looks like the boring part of this project. It is where three of
its sharper edges live.

**`CreateQueue` is idempotent.** Creating a queue that already exists with the
*same* attributes succeeds and returns the same URL; creating it with *different*
attributes is a conflict. That one rule is what lets a Terraform run, a CI job, or
a service's startup path call `CreateQueue` unconditionally, a thousand times,
without a "does it exist?" branch that has its own race. Getting it right is
fiddlier than it sounds: which attributes participate in the comparison? What
about ones the caller did not send — do they default and match, or is an omitted
attribute a wildcard? Answer those two questions and you have designed the
property; get either wrong and you have built an API that fails on the second
deploy.

**Attribute changes and existing messages.** Lower a visibility timeout while
messages are in flight: do the outstanding leases shorten, or do they stand?
Shorten retention below the age of messages already in the queue: do they vanish
immediately? Both have a defensible answer and an indefensible one, and the SPEC
asks for a table — attribute by attribute, what happens to messages already
there — that the implementation actually matches. This is the kind of thing that
is obvious to whoever wrote it and unknowable to everyone else.

**Redrive** is where V1's receive count pays off. A message received more than
`maxReceiveCount` times goes to the dead-letter queue instead of being delivered
again — that is the whole poison-message defence, and note what it is *not*: SQS
has no retry schedule and no exponential backoff. A consumer that wants backoff
expresses it by extending visibility. Decide whether you agree with that design
(project 04 made the opposite choice, with backoff and jitter in the broker) and
write down which belongs where.

Then build the way **back**. A dead-letter queue you can put messages into and
not get them out of is a data-loss bug with a friendly name. Real SQS grew a
whole redrive API for this years after the fact, which tells you how often people
discovered they needed it.

Scaffold state: the store's create/lookup plumbing lives in `state.py` and works;
the *semantics* — idempotency, validation, apply-to-existing, redrive — raise.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .config import Settings
from .models import Message, QueueAttributes

__all__ = ["AttributeApplication", "ControlPlane", "RedriveDecision"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AttributeApplication:
    """The answer to "does this change affect messages already in the queue?".

    Modelled rather than left implicit because the SPEC grades the *table*, and a
    table that lives only in a markdown file drifts from the code within a month.
    Returning this from the validation path means the document and the behaviour
    are the same object.
    """

    attribute: str
    applies_to_existing: bool
    # One sentence a reviewer could disagree with. If you cannot write it, the
    # decision has not been made yet.
    rationale: str


@dataclass(frozen=True, slots=True)
class RedriveDecision:
    """Whether a message has run out of chances.

    `receive_count` is included so the decision is auditable after the fact: a
    message in a DLQ with no record of how it got there is a message someone
    will redrive straight back into the same failure.
    """

    to_dlq: bool
    dlq_arn: str | None
    receive_count: int


class ControlPlane:
    """Queue lifecycle, attribute semantics, and the redrive path."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def normalize_attributes(self, name: str, requested: dict[str, str] | None) -> QueueAttributes:
        """Turn the wire's string map into a validated, defaulted attribute set.

        Every cap in `Settings` is enforced here — at **write** time. An attribute
        validated at delivery time instead is one that fails on the hot path,
        where the only safe answer left is to refuse a message the client already
        believes it sent.

        FIFO-only attributes on a standard queue belong here too: accepting
        `ContentBasedDeduplication` on a standard queue and quietly ignoring it is
        how a team spends a week wondering why their duplicates are not being
        removed.
        """
        # TODO(V6): parse, validate against the ceilings, and default. Refuse
        # unknown names rather than ignoring them.
        raise NotImplementedError("V6: normalize and validate queue attributes")

    def creation_conflicts(self, existing: QueueAttributes, requested: QueueAttributes) -> bool:
        """Does this `CreateQueue` conflict with the queue that already exists?

        The idempotency rule in one function. Decide what an *omitted* attribute
        means before you write it — matching the default, or matching anything —
        because that choice is the difference between a deploy pipeline that is
        safe to re-run and one that fails the second time on a queue nobody
        touched.
        """
        # TODO(V6): compare the attribute sets that participate in identity.
        raise NotImplementedError("V6: decide whether a re-create conflicts")

    def application_table(self) -> list[AttributeApplication]:
        """The applies-to-existing decisions, as data. Feeds the design doc.

        Fill this in as you make each call, not afterwards. It is also the
        natural place for a test to assert that the implementation matches the
        table — which is the only way the table stays true.
        """
        # TODO(V6): return the per-attribute decisions this implementation makes.
        raise NotImplementedError("V6: report the attribute application table")

    def apply_attributes(
        self,
        current: QueueAttributes,
        changes: QueueAttributes,
        messages: list[Message],
        now: float,
    ) -> QueueAttributes:
        """Apply an attribute change, including to messages already in the queue.

        Where the table becomes behaviour. Note that `messages` includes in-flight
        ones, and that touching their deadlines means going through V2's engine —
        moving a visibility deadline here has exactly the same cancel-and-
        reschedule requirements it has anywhere else.
        """
        # TODO(V6): apply the change, and touch existing messages only where the
        # table says you should.
        raise NotImplementedError("V6: apply an attribute change")

    def redrive_check(self, message: Message, attributes: QueueAttributes) -> RedriveDecision:
        """Has this message been delivered too many times?

        Called on the receive path, *before* the message is handed out — a message
        that has already hit its limit must not be delivered a final time on its
        way to the DLQ. The off-by-one here is worth being deliberate about: with
        `maxReceiveCount = 3`, does the consumer see it three times or four? Write
        the test that pins your answer.
        """
        # TODO(V6): compare the receive count against the redrive policy.
        raise NotImplementedError("V6: decide whether a message should be dead-lettered")

    def move_to_dlq(self, message: Message, dlq_name: str, now: float) -> Message:
        """Move a message to its dead-letter queue.

        Preserve what an operator will need: the original queue, the original
        send time, and the receive count that got it here. A DLQ full of messages
        with fresh timestamps and no provenance is a DLQ nobody can triage.
        """
        # TODO(V6): move the message, keeping its history.
        raise NotImplementedError("V6: move a message to the dead-letter queue")

    def redrive_back(self, dlq_name: str, source_name: str, limit: int, now: float) -> int:
        """Move messages from a DLQ back to their source queue. Returns how many.

        The way out. Think about what a redrive must reset (the receive count —
        or the message goes straight back to the DLQ on its first delivery) and
        what it must not (the message id, or every consumer's idempotency key
        changes underneath them).
        """
        # TODO(V6): move messages back, resetting only what must be reset.
        raise NotImplementedError("V6: redrive messages from a DLQ to its source")
