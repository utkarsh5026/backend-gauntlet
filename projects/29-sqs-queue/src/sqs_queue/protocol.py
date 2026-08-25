"""The AWS JSON protocol envelope. Plumbing — fully implemented.

Modern SQS speaks **AWS JSON 1.0**: one endpoint, `POST /`, with the verb in an
`X-Amz-Target: AmazonSQS.<Action>` header and a JSON body. (It used to speak the
Query protocol — form-encoded, `Action=SendMessage` — which is what project 25's
IAM surface still uses. Both are AWS's, a decade apart, and the shift is worth
noticing: the verb moved out of the body and into a header, and the body became a
document instead of a flattened parameter list.)

This module knows about the *envelope* and nothing about queues: how to read the
target header, which actions exist, and how a batch request is shaped. Parameter
meaning, limits and semantics belong to the verticals — a protocol layer that
starts validating visibility timeouts is a protocol layer that will disagree with
the control plane about what the limit is.

**The batch shape is the interesting part.** `SendMessageBatch` and friends take
up to 10 entries and answer with **both** a `Successful` and a `Failed` list, at
HTTP 200, even when some entries failed. That is unusual enough to be worth
saying twice: partial failure is the normal case, not an error case. A batch API
that is all-or-nothing forces the client to re-send nine good messages because of
one bad one — and if it retries the whole batch, it has just enqueued nine
duplicates.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, cast

from .errors import (
    BatchEntryIdsNotDistinct,
    EmptyBatchRequest,
    InvalidBatchEntryId,
    InvalidParameterValue,
)

__all__ = [
    "TARGET_HEADER",
    "TARGET_PREFIX",
    "Action",
    "BatchEntry",
    "BatchResult",
    "parse_batch_entries",
    "parse_target",
]

TARGET_HEADER = "x-amz-target"
TARGET_PREFIX = "AmazonSQS."

# Batch entry ids are the client's own correlation keys — it uses them to match
# results back to what it sent. Bounded and charset-restricted because they come
# straight back in the response, and an unbounded echo is an amplification.
MAX_BATCH_ENTRY_ID_LENGTH = 80


class Action(enum.StrEnum):
    """Every action this service answers.

    An explicit enum rather than `getattr(handlers, action)`: a dispatcher that
    reaches into a namespace with a caller-supplied string is one typo away from
    being an arbitrary-call gadget, and an unknown action should be a clean
    error rather than an `AttributeError` in a traceback.
    """

    CREATE_QUEUE = "CreateQueue"
    GET_QUEUE_URL = "GetQueueUrl"
    LIST_QUEUES = "ListQueues"
    GET_QUEUE_ATTRIBUTES = "GetQueueAttributes"
    SET_QUEUE_ATTRIBUTES = "SetQueueAttributes"
    DELETE_QUEUE = "DeleteQueue"
    PURGE_QUEUE = "PurgeQueue"

    SEND_MESSAGE = "SendMessage"
    SEND_MESSAGE_BATCH = "SendMessageBatch"
    RECEIVE_MESSAGE = "ReceiveMessage"
    DELETE_MESSAGE = "DeleteMessage"
    DELETE_MESSAGE_BATCH = "DeleteMessageBatch"
    CHANGE_MESSAGE_VISIBILITY = "ChangeMessageVisibility"
    CHANGE_MESSAGE_VISIBILITY_BATCH = "ChangeMessageVisibilityBatch"

    # The way out of a dead-letter queue (V6). Real SQS grew this years after the
    # DLQ itself, which tells you how the omission was discovered.
    START_MESSAGE_MOVE_TASK = "StartMessageMoveTask"


def parse_target(header_value: str | None) -> Action:
    """Read `X-Amz-Target` and resolve the action.

    Raises `InvalidParameterValue` for a missing, misprefixed or unknown target.
    Note that this happens *before* any queue is looked up, so a caller who sends
    garbage learns only that the target was bad.
    """
    if not header_value:
        raise InvalidParameterValue(f"missing {TARGET_HEADER} header")
    name = header_value.split(".", 1)[-1] if header_value.startswith(TARGET_PREFIX) else None
    if name is None:
        raise InvalidParameterValue(f"{TARGET_HEADER} must be '{TARGET_PREFIX}<Action>'")
    try:
        return Action(name)
    except ValueError:
        raise InvalidParameterValue(f"unknown action {name!r}") from None


@dataclass(slots=True)
class BatchEntry:
    """One entry in a batch request, with the client's correlation id."""

    entry_id: str
    body: dict[str, Any]


@dataclass(slots=True)
class BatchResult:
    """The two-list answer a batch action returns.

    Both lists, always, at HTTP 200. A `Failed` entry carries the entry id, an
    error code, and whether the *sender* was at fault — that last flag is how a
    client knows whether retrying this entry could ever work.
    """

    successful: list[dict[str, Any]]
    failed: list[dict[str, Any]]

    def to_response(self, success_key: str = "Successful") -> dict[str, Any]:
        return {success_key: self.successful, "Failed": self.failed}


def parse_batch_entries(body: dict[str, Any], max_entries: int) -> list[BatchEntry]:
    """Pull `Entries` out of a batch request and check the envelope rules.

    Structural checks only — empty, too many, duplicate or malformed ids. What is
    *in* an entry is the action's business: a `SendMessageBatch` entry with an
    oversized body is a **per-entry failure** in the `Failed` list, not a rejected
    batch, and conflating the two throws away nine good messages.
    """
    raw = body.get("Entries")
    if not isinstance(raw, list) or not raw:
        raise EmptyBatchRequest()
    entries_in = cast(list[Any], raw)
    if len(entries_in) > max_entries:
        raise InvalidParameterValue(f"a batch may contain at most {max_entries} entries")

    entries: list[BatchEntry] = []
    seen: set[str] = set()
    for item in entries_in:
        if not isinstance(item, dict):
            raise InvalidBatchEntryId("batch entries must be objects")
        entry = cast(dict[str, Any], item)
        entry_id = entry.get("Id")
        if not isinstance(entry_id, str) or not entry_id:
            raise InvalidBatchEntryId("every batch entry needs an Id")
        if len(entry_id) > MAX_BATCH_ENTRY_ID_LENGTH:
            raise InvalidBatchEntryId(
                f"batch entry ids are at most {MAX_BATCH_ENTRY_ID_LENGTH} characters"
            )
        if entry_id in seen:
            raise BatchEntryIdsNotDistinct(f"duplicate batch entry id {entry_id!r}")
        seen.add(entry_id)
        entries.append(BatchEntry(entry_id=entry_id, body=entry))
    return entries
