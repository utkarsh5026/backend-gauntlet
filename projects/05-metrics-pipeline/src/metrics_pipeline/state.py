"""The objects every handler needs, assembled once at startup.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from clickhouse_connect.driver.asyncclient import AsyncClient

from .broker import Producer
from .config import Settings
from .sse import LiveFeed

__all__ = ["AppState"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    producer: Producer
    """Publishes ingested lines to the durable stream."""
    feed: LiveFeed
    """The SSE fan-out hub, shared with the consumer pipeline."""
    clickhouse: AsyncClient | None
    """Read-only handle for `GET /query`. `None` when ClickHouse was unreachable
    at startup — the query path then 503s while the rest of the app still serves."""

    @property
    def rollup_table(self) -> str:
        return self.settings.rollup_table
