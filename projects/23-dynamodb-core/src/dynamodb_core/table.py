"""V1 — The item model & primary key.

The partition key is not a lookup optimisation. It decides *where the item
physically lives*, and every surprising thing about DynamoDB's API follows from
that one fact: `Query` demands a partition key because without one there is no
partition to read; `Scan` exists as the expensive apology; and two items with the
same partition key are neighbours on disk, which is why a range read over a sort
key is cheap and a range read over anything else is not.

The other half of this vertical is **key encoding**. A sort key has to be totally
ordered, and the ordering has to survive being written to disk. Strings are easy.
Numbers are not: `"10"` sorts before `"9"` lexicographically, and a JSON float
loses precision on large integers, so the encoding has to be order-preserving over
`decimal.Decimal`. Get that wrong and every range query is subtly incorrect.

Scaffold state: the table is constructed and registered, but every operation
raises. The first PutItem that reaches it blows up — that is your worklist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from .item import AttributeValue, Item, ItemKey, KeySchema, TableName

__all__ = [
    "ComparisonOperator",
    "Page",
    "SortKeyCondition",
    "Table",
    "TableDefinition",
    "encode_key",
]


class ComparisonOperator(StrEnum):
    """The range conditions a `Query` may apply to the sort key.

    Note what is *absent*: there is no `contains` and no `ends_with`. A sorted
    structure can answer prefixes and ranges cheaply and nothing else — the API's
    limits are the data structure's limits, which is the point.
    """

    EQ = "="
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    BETWEEN = "between"
    BEGINS_WITH = "begins_with"


class SortKeyCondition(BaseModel):
    """A range restriction within one partition."""

    operator: ComparisonOperator
    value: AttributeValue
    # Only used by BETWEEN, which is the one two-sided operator.
    upper: AttributeValue | None = None


class TableDefinition(BaseModel):
    """What a table is created with. Immutable for the life of the table —
    changing a key schema means building a new table and migrating, which is a
    constraint worth feeling rather than reading about.
    """

    name: TableName
    key_schema: KeySchema
    read_capacity: int = Field(default=1000, gt=0)
    write_capacity: int = Field(default=1000, gt=0)


@dataclass(slots=True)
class Page:
    """One page of a Query or Scan.

    `last_evaluated_key` is `None` exactly when the result is complete — that is
    the contract the pagination horizontal is graded on, and it is *not* the same
    as "the page came back empty" (a filtered Scan can legitimately return zero
    items and still have more to read).
    """

    items: list[Item]
    last_evaluated_key: ItemKey | None = None
    scanned_count: int = 0


def encode_key(value: AttributeValue) -> bytes:
    """Encode a key attribute into **order-preserving** bytes.

    The whole of V1's correctness rests here: `encode_key(a) < encode_key(b)` must
    hold exactly when `a` sorts before `b` in DynamoDB's ordering, for every pair
    of values of the same type.
    """
    # TODO(V1): handle S, N and B (the only legal key types — see item.KEY_TYPES).
    #  - S: UTF-8 bytes already sort correctly.
    #  - N: the hard one. Parse with `decimal.Decimal` (never `float` — it rounds
    #    large integers silently), then design an encoding where negative numbers
    #    sort before positive ones and "10" does not sort before "9". Sign flag +
    #    biased exponent + normalised digits is the classic shape.
    #  - B: raw bytes.
    # Property-test it: for random same-type pairs, byte order must match value
    # order. That test is the V1 proof.
    raise NotImplementedError("V1: order-preserving key encoding for S / N / B")


class Table:
    """One table: its definition, its items, and the two read paths.

    The storage layout is yours to choose, and it is the decision the SPEC grades.
    The shape that makes `Query` cheap is a map from encoded partition key to that
    partition's items held in **sort-key order** — so a range read is a slice, not
    a filter over everything.
    """

    def __init__(self, definition: TableDefinition) -> None:
        self._definition = definition
        # TODO(V1): your storage lives here. To make Query O(matched) rather than
        # O(table), you want partitions to be addressable and each partition's
        # items kept sorted by encoded sort key:
        #
        #   self._partitions: dict[bytes, list[tuple[bytes, Item]]]
        #
        # with `bisect.insort` to insert and `bisect.bisect_left/right` to find a
        # range's bounds. (A plain dict keyed by (pk, sk) is a fine first step in
        # step 1 of the order of attack — but it cannot answer a range query
        # without scanning, which is exactly the property this vertical is about.)

    @property
    def definition(self) -> TableDefinition:
        return self._definition

    @property
    def key_schema(self) -> KeySchema:
        return self._definition.key_schema

    def put_item(self, item: Item) -> Item | None:
        """Insert or overwrite. Returns the previous item, if any.

        The old item is not a convenience — V2 needs it to remove stale index
        entries and V5 needs it for a `MODIFY` record's `OLD_IMAGE`.
        """
        # TODO(V1): validate the item carries every key attribute and that key
        # attributes are legal key types; enforce the size cap BEFORE storing;
        # encode the key; place it in the right partition in sort order.
        raise NotImplementedError("V1: validated, size-capped put into the right partition")

    def get_item(self, key: ItemKey) -> Item | None:
        """Point read by **full** primary key. A partial key is an error, not a scan."""
        raise NotImplementedError("V1: point read by full primary key")

    def delete_item(self, key: ItemKey) -> Item | None:
        """Remove by full primary key; returns the item that was there, if any."""
        raise NotImplementedError("V1: delete by full primary key, returning the old item")

    def query(
        self,
        partition: AttributeValue,
        *,
        sort_condition: SortKeyCondition | None = None,
        forward: bool = True,
        limit: int | None = None,
        start_key: ItemKey | None = None,
    ) -> Page:
        """Every item under one partition key, in sort-key order.

        `forward=False` reads the partition backwards — which is how you get "the
        most recent N" without storing a reversed copy.
        """
        # TODO(V1): locate the partition, then use the sorted order to find the
        # range's bounds directly (bisect), rather than filtering the partition.
        # Honour `limit` and return a `last_evaluated_key` when you stop early —
        # a caller must be able to resume exactly where you left off.
        raise NotImplementedError("V1: ordered, range-restricted read within one partition")

    def scan(self, *, limit: int | None = None, start_key: ItemKey | None = None) -> Page:
        """Every item in the table. The expensive apology.

        Set `scanned_count` even when items are filtered out — the gap between
        "scanned" and "returned" is the number that teaches people to stop using
        Scan, and it is what the consumed-capacity model (V4) charges on.
        """
        raise NotImplementedError("V1: full-table read with pagination and a scanned count")

    def __len__(self) -> int:
        """Live item count — backs the item-count metric and the capacity tests."""
        raise NotImplementedError("V1: count of items across every partition")
