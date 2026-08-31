"""Batch requests, and the thing about them people get wrong.

`SendMessageBatch` and its siblings take up to 10 entries and answer with **both**
a `Successful` and a `Failed` list, at HTTP 200, even when some entries failed.
That is unusual enough to be worth saying twice: partial failure is the normal
case here, not an error case.

The alternative — all-or-nothing — looks tidier and is worse. It forces a client
to re-send nine good messages because of one bad one, and if it retries the whole
batch it has just enqueued nine duplicates. So the batch envelope is the only
place in this package where a failure is not an exception: a per-entry error is
*data*, carried back in a 200, with enough information (`SenderFault`) for the
client to know which entries are worth retrying.

Structural checks live here — empty batch, too many entries, duplicate or
malformed ids. What is *in* an entry is the action's business: an oversized
message body is a per-entry failure, not a rejected batch, and conflating the two
throws away nine good messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from .errors import AwsError, InvalidParameterValue, ValidationException

__all__ = [
    "MAX_BATCH_ENTRIES",
    "MAX_BATCH_ENTRY_ID_LENGTH",
    "BatchEntry",
    "BatchResult",
    "parse_batch_entries",
]

# AWS's own cap, across every batch API it has. Ten is not a technical limit; it
# is a product decision that bounds the blast radius of one request.
MAX_BATCH_ENTRIES = 10

# Batch entry ids are the client's own correlation keys — it uses them to match
# results back to what it sent. Bounded and charset-restricted because they come
# straight back in the response, and an unbounded echo is an amplification.
MAX_BATCH_ENTRY_ID_LENGTH = 80


@dataclass(slots=True)
class BatchEntry:
    """One entry in a batch request, with the client's correlation id."""

    entry_id: str
    body: dict[str, Any]


@dataclass(slots=True)
class BatchResult:
    """The two-list answer a batch action returns. Both lists, always, at 200."""

    successful: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    failed: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def succeed(self, entry_id: str, **fields: Any) -> None:
        """Record a successful entry, echoing its id back to the client."""
        self.successful.append({"Id": entry_id, **fields})

    def fail(self, entry_id: str, error: AwsError) -> None:
        """Record a failed entry in the shape AWS uses.

        `SenderFault` is the field that earns its place: it tells the client
        whether retrying this entry could ever work, without it having to keep a
        table of which error codes are the caller's fault.
        """
        self.failed.append(
            {
                "Id": entry_id,
                "Code": error.error_code,
                "Message": error.safe_message,
                "SenderFault": error.sender_fault,
            }
        )

    def to_response(self, success_key: str = "Successful") -> dict[str, Any]:
        """Both lists, even when one is empty — the client indexes into them."""
        return {success_key: self.successful, "Failed": self.failed}


def parse_batch_entries(
    body: dict[str, Any],
    *,
    max_entries: int = MAX_BATCH_ENTRIES,
    entries_key: str = "Entries",
) -> list[BatchEntry]:
    """Pull the entries out of a batch request and check the envelope rules.

    Raises `ValidationException` for an empty batch and `InvalidParameterValue`
    for a batch that is too large or whose ids are malformed or duplicated. A
    service with its own named errors for these (SQS has `EmptyBatchRequest`,
    `InvalidBatchEntryId`, `BatchEntryIdsNotDistinct`) should catch and re-raise,
    or subclass these — the codes are the part a client reads.
    """
    raw = body.get(entries_key)
    if not isinstance(raw, list) or not raw:
        raise ValidationException(f"{entries_key} must be a non-empty list")
    entries_in = cast(list[Any], raw)
    if len(entries_in) > max_entries:
        raise InvalidParameterValue(f"a batch may contain at most {max_entries} entries")

    entries: list[BatchEntry] = []
    seen: set[str] = set()
    for item in entries_in:
        if not isinstance(item, dict):
            raise InvalidParameterValue("batch entries must be objects")
        entry = cast(dict[str, Any], item)
        entry_id = entry.get("Id")
        if not isinstance(entry_id, str) or not entry_id:
            raise InvalidParameterValue("every batch entry needs an Id")
        if len(entry_id) > MAX_BATCH_ENTRY_ID_LENGTH:
            raise InvalidParameterValue(
                f"batch entry ids are at most {MAX_BATCH_ENTRY_ID_LENGTH} characters"
            )
        if entry_id in seen:
            raise InvalidParameterValue(f"duplicate batch entry id {entry_id!r}")
        seen.add(entry_id)
        entries.append(BatchEntry(entry_id=entry_id, body=entry))
    return entries
