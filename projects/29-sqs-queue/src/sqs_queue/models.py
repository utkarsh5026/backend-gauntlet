"""The domain types every module shares. Plumbing — fully implemented.

Nothing here decides anything; these are the shapes the verticals move around.
Two of them are worth reading closely before you start, because they encode
decisions the SPEC grades.

`Message.generation` is V1's whole idea in one integer. It increments on every
delivery, and a receipt handle names a `(message_id, generation)` pair. A handle
from generation 3 presented after the message has moved on to generation 4 is
*stale by construction* — you do not have to track who holds what, you just
compare. The same integer is what makes V2's timer races decidable: a visibility
deadline scheduled for generation 3 that fires when the message is at generation
4 is a deadline for a delivery that is already over, and it is dropped without
touching anything.

`MessageState` is deliberately explicit rather than derived from timestamps. A
state you compute from `visible_after < now()` is a state that two pieces of code
will compute slightly differently, and the disagreement will be a message that is
in flight according to the receiver and available according to the metrics.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "Delivery",
    "Message",
    "MessageAttributeValue",
    "MessageState",
    "QueueAttributes",
    "QueueKind",
    "ReceivedMessage",
    "SendResult",
]


class QueueKind(enum.StrEnum):
    """Standard or FIFO. Fixed at creation and never changeable.

    Not an attribute you can flip: the ordering guarantee is a property clients
    build on, and a queue that silently stopped being FIFO would break them in a
    way no error message would ever explain. Real SQS encodes it in the *name*
    (`.fifo`) for exactly this reason — the contract is visible at every call
    site.
    """

    STANDARD = "standard"
    FIFO = "fifo"


class MessageState(enum.StrEnum):
    """Where a message is in its lifecycle.

    `DELAYED` and `AVAILABLE` are separate because they are separate numbers to a
    consumer staring at a dashboard: a queue with 10,000 delayed messages is
    working as designed, and one with 10,000 available messages and no receives
    is an outage.
    """

    DELAYED = "delayed"
    AVAILABLE = "available"
    INFLIGHT = "inflight"
    DELETED = "deleted"


class MessageAttributeValue(BaseModel):
    """Typed metadata riding alongside the body.

    The `DataType` is part of the signed/hashed content in real SQS, which is why
    it is modelled rather than flattened to a string: two messages with the same
    bytes and different declared types are different messages, and V5's
    content-based dedup has to agree with that.
    """

    DataType: Literal["String", "Number", "Binary"] = "String"
    StringValue: str | None = None
    BinaryValue: bytes | None = None


@dataclass(slots=True)
class QueueAttributes:
    """A queue's configuration. V6 owns the rules; this is the bag.

    Every field here is settable through `SetQueueAttributes`, and every one has
    a documented ceiling in `Settings`. The interesting question the SPEC asks is
    not what these values are — it is which of them affect messages **already in
    the queue** when they change.
    """

    visibility_timeout_seconds: float = 30.0
    receive_wait_time_seconds: float = 0.0
    delay_seconds: float = 0.0
    retention_seconds: float = 345_600.0
    max_message_bytes: int = 262_144
    # Redrive: after this many deliveries a message goes to `dlq_arn` instead of
    # being delivered again. Both are None on a queue with no DLQ configured,
    # which means a poison message is delivered forever — the default that the
    # dead-letter queue exists to fix.
    max_receive_count: int | None = None
    dlq_arn: str | None = None
    # FIFO only. Content-based dedup derives V5's dedup id from the body when the
    # client does not supply one.
    content_based_deduplication: bool = False


@dataclass(slots=True)
class Delivery:
    """One handing-out of a message. V1's unit of accounting.

    Kept as its own object rather than a few fields on `Message` because the
    receipt handle names *this*, not the message: two deliveries of the same
    message are two different things that can each be deleted, extended, or
    allowed to expire, and only one of them is current.
    """

    generation: int
    receipt_handle: str
    received_at: float
    visible_after: float
    # Which consumer, when you know. Not used for authorization — a handle is the
    # authorization — but invaluable in the audit trail when a queue is stuck and
    # you need to know who is holding it.
    consumer_hint: str | None = None


@dataclass(slots=True)
class Message:
    """A message and everything the service knows about it.

    `generation` and `receive_count` look redundant and are not. A generation is
    incremented by every delivery *and* by anything that invalidates outstanding
    handles; `receive_count` is what V6's redrive policy counts against, and it
    must reflect deliveries only. Conflating them is how a message gets
    dead-lettered because somebody called `ChangeMessageVisibility`.
    """

    message_id: str
    queue_name: str
    body: str
    md5_of_body: str
    sent_at: float
    # First moment this may be delivered. Set by `DelaySeconds` at send time and
    # moved by visibility changes afterwards.
    visible_after: float
    expires_at: float
    state: MessageState = MessageState.AVAILABLE
    generation: int = 0
    receive_count: int = 0
    attributes: dict[str, MessageAttributeValue] = field(
        default_factory=dict[str, "MessageAttributeValue"]
    )
    current_delivery: Delivery | None = None

    # --- FIFO (V4, V5) ------------------------------------------------------
    group_id: str | None = None
    dedup_id: str | None = None
    # Strictly increasing within a group. The wire calls it `SequenceNumber` and
    # returns it as a decimal string, because it outgrew 64 bits.
    sequence_number: int | None = None


class SendResult(BaseModel):
    """What `SendMessage` answers.

    `deduplicated` is not part of the AWS response — it is here because V5's
    first criterion is that a duplicate send is *acknowledged with the original
    message id*, and a test needs to be able to tell the two cases apart. Keep it
    off the wire when you build the protocol layer, or you have invented an API
    that tells clients about your internals.
    """

    message_id: str
    md5_of_body: str
    sequence_number: str | None = None
    deduplicated: bool = False


class ReceivedMessage(BaseModel):
    """One message as a consumer sees it.

    The consumer gets a `receipt_handle`, never a way to address the message
    generically — that asymmetry *is* V1. If you find yourself adding a field
    here that lets a caller act on a message without its handle, you have
    reopened the stale-delete race.
    """

    message_id: str
    receipt_handle: str
    body: str
    md5_of_body: str
    receive_count: int = Field(ge=1)
    sent_at: float
    group_id: str | None = None
    sequence_number: str | None = None
    attributes: dict[str, MessageAttributeValue] = {}
