"""The attribute vocabulary every other module agrees on.

Not a vertical of its own — this is the shape of a DynamoDB item, which the table
(V1), the indexes (V2), the condition evaluator (V3) and the stream (V5) all speak.

DynamoDB wire format is *typed JSON*: an attribute is a single-entry map whose key
names the type, e.g. `{"S": "cust#1"}` or `{"N": "42"}`. Numbers arrive as
**strings** on purpose — the real service stores them as fixed-point decimals, and
a JSON float would silently round `12345678901234567890`. In Python that means
`decimal.Decimal`, never `float`, anywhere a stored number is parsed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, NamedTuple

from pydantic import BaseModel

__all__ = [
    "AttributeType",
    "AttributeValue",
    "Item",
    "ItemKey",
    "KeySchema",
    "TableName",
    "key_of",
]

TableName = str

# One typed attribute, e.g. {"S": "abc"} / {"N": "42"} / {"M": {...}}.
AttributeValue = dict[str, Any]

# A whole item: attribute name -> typed value.
Item = dict[str, AttributeValue]


class AttributeType(StrEnum):
    """The type tags DynamoDB puts on the wire."""

    STRING = "S"
    NUMBER = "N"
    BINARY = "B"
    BOOL = "BOOL"
    NULL = "NULL"
    LIST = "L"
    MAP = "M"
    STRING_SET = "SS"
    NUMBER_SET = "NS"
    BINARY_SET = "BS"


# Only these three can be a key attribute: a key has to be totally ordered and
# fixed-width-comparable, which rules out documents and sets.
KEY_TYPES = frozenset({AttributeType.STRING, AttributeType.NUMBER, AttributeType.BINARY})


class KeySchema(BaseModel):
    """Which attributes form a table's (or index's) primary key.

    `sort_key is None` means a simple key: one item per partition key. With a sort
    key, a partition holds many items and `Query` can range over them.
    """

    partition_key: str
    sort_key: str | None = None

    @property
    def is_composite(self) -> bool:
        return self.sort_key is not None


class ItemKey(NamedTuple):
    """The *values* of an item's key attributes, as they came off the wire."""

    partition: AttributeValue
    sort: AttributeValue | None = None


def key_of(item: Item, schema: KeySchema) -> ItemKey:
    """Pull the key attributes out of an item.

    Plumbing — extracting named fields is not the interesting part. Turning these
    values into something *sortable* is V1's job (see `table.py`).

    Raises `KeyError` if the item is missing a key attribute; callers surface that
    as a validation error rather than storing a keyless item.
    """
    partition = item[schema.partition_key]
    if schema.sort_key is None:
        return ItemKey(partition)
    return ItemKey(partition, item[schema.sort_key])
