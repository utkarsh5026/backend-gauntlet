"""V2 — Secondary indexes.

An index is not a query hint. It is a **second copy of your data**, keyed
differently, that something has to keep up to date on every write — which is why
each index multiplies your write cost, and why the two kinds behave differently:

  * **LSI** keeps the partition key and changes only the sort key. The entry lands
    in the *same* partition as the base item, so it can be written in the same
    atomic step and read back **strongly consistent**.
  * **GSI** re-partitions under a different key entirely. Its entry belongs to a
    different partition, so it cannot join the base write's atomic step without
    paying a cross-partition commit on every single write. DynamoDB refuses that
    trade, maintains the GSI **after the fact**, and hands you eventual consistency
    plus a separate capacity budget. That is the whole reason a GSI can lag.

Scaffold state: definitions are modelled; maintenance and the index read path raise.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .item import AttributeValue, Item, KeySchema
from .table import Page, SortKeyCondition

__all__ = ["IndexDefinition", "IndexType", "ProjectionType", "SecondaryIndex"]


class IndexType(StrEnum):
    LOCAL = "LSI"
    GLOBAL = "GSI"


class ProjectionType(StrEnum):
    """How much of the base item is copied into the index.

    This is a storage-cost dial. `ALL` makes every index read self-sufficient and
    doubles your storage; `KEYS_ONLY` is cheap but forces a second read against the
    base table for anything else — the "index fetch" that quietly doubles latency.
    """

    KEYS_ONLY = "KEYS_ONLY"
    INCLUDE = "INCLUDE"
    ALL = "ALL"


class IndexDefinition(BaseModel):
    name: str
    index_type: IndexType
    key_schema: KeySchema
    projection: ProjectionType = ProjectionType.KEYS_ONLY
    # Only meaningful for INCLUDE.
    projected_attributes: list[str] = []
    # A GSI has its own capacity; an LSI draws on the base table's.
    read_capacity: int | None = Field(default=None, gt=0)
    write_capacity: int | None = Field(default=None, gt=0)


class SecondaryIndex:
    """One index over a base table.

    Holds its own entries — an index read must never fall back to scanning the
    base table, or it isn't an index.
    """

    def __init__(self, definition: IndexDefinition, base_key_schema: KeySchema) -> None:
        self._definition = definition
        self._base_key_schema = base_key_schema
        # TODO(V2): index storage. The entries are just items in a differently
        # keyed table, so the same partition-plus-sorted-list shape from V1 works
        # here. Reusing `Table` itself is a legitimate design choice — say so in
        # the design doc if you take it.
        #
        # An index entry must also carry the BASE table's key, or a KEYS_ONLY hit
        # can never be resolved back to the full item.

    @property
    def definition(self) -> IndexDefinition:
        return self._definition

    @property
    def is_consistent(self) -> bool:
        """LSI reads can be strongly consistent; GSI reads cannot. Ever."""
        return self._definition.index_type is IndexType.LOCAL

    def project(self, item: Item) -> Item:
        """Cut a base item down to exactly what this index promises to store."""
        # TODO(V2): honour KEYS_ONLY / INCLUDE / ALL. Always keep the index's own
        # key attributes AND the base table's key attributes, whatever the
        # projection says — without them the entry is unusable.
        raise NotImplementedError("V2: project a base item down to this index's attributes")

    def maintain(self, old_item: Item | None, new_item: Item | None) -> None:
        """Bring the index in line with one base-table mutation.

        `(None, item)` is an insert, `(item, None)` a delete, `(old, new)` an update.
        """
        # TODO(V2): the case that catches everyone — an update that CHANGES the
        # indexed attribute is a delete of the old entry plus an insert of the new
        # one. Handle it and you have no orphans; miss it and the index quietly
        # returns items that no longer match.
        #
        # Sparse indexes: if an item has no value for this index's key attribute,
        # it simply does not appear here. That is a feature (it is how you index
        # "only the rows in state X"), not an error.
        raise NotImplementedError("V2: apply an insert/update/delete to this index")

    def query(
        self,
        partition: AttributeValue,
        *,
        sort_condition: SortKeyCondition | None = None,
        forward: bool = True,
        limit: int | None = None,
    ) -> Page:
        """Read through the index's own key."""
        # TODO(V2): same read path as Table.query, over this index's entries.
        # Reject a request for a consistent read on a GSI rather than silently
        # serving a stale one — the API says no for a reason.
        raise NotImplementedError("V2: ordered read through the index's key")
