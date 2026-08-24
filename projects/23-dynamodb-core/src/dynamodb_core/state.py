"""The objects every handler needs, assembled once at startup.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).

Everything here is **plumbing**: registering a table and looking one up is not a
vertical. The interesting behaviour lives behind the objects this holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .errors import ResourceNotFound
from .indexes import SecondaryIndex
from .item import TableName
from .streams import Stream
from .table import Table, TableDefinition
from .throughput import PartitionLimiter

__all__ = ["AppState", "Catalog", "TableContext"]


@dataclass(slots=True)
class TableContext:
    """Everything that belongs to one table: its data, indexes, stream, budget."""

    table: Table
    indexes: dict[str, SecondaryIndex] = field(default_factory=dict[str, SecondaryIndex])
    stream: Stream | None = None
    read_limiter: PartitionLimiter | None = None
    write_limiter: PartitionLimiter | None = None
    # TODO(V4): the per-PARTITION limiters live here too — one bucket per
    # partition key, created on first touch and (importantly) evicted when idle,
    # or a table with millions of keys leaks a bucket per key. A bounded LRU of
    # buckets is the usual answer; note that evicting a bucket forgives its debt,
    # which is a tradeoff worth recording in the design doc.


class Catalog:
    """The set of tables this node serves."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tables: dict[TableName, TableContext] = {}

    def create_table(self, definition: TableDefinition) -> TableContext:
        """Register a new table. Plumbing — the storage behaviour is V1's."""
        if definition.name in self._tables:
            raise ValueError(f"table {definition.name!r} already exists")
        context = TableContext(table=Table(definition))
        self._tables[definition.name] = context
        return context

    def get(self, name: TableName) -> TableContext:
        try:
            return self._tables[name]
        except KeyError:
            raise ResourceNotFound(f"table {name!r} not found") from None

    def names(self) -> list[TableName]:
        return sorted(self._tables)

    def __len__(self) -> int:
        return len(self._tables)


@dataclass(slots=True)
class AppState:
    settings: Settings
    catalog: Catalog
