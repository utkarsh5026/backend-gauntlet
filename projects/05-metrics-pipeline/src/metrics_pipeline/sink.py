"""V3 — The durable, batched sink: at-least-once into ClickHouse.

Get rollups out of memory and into the column store **in batches** and
**durably**. A column store like ClickHouse is built for big appends and falls
over on row-at-a-time inserts, so the lesson is micro-batching: buffer rollups
and flush on a **size or time** trigger. Durability is at-least-once — the
pipeline acks the broker message only *after* a batch is safely written, which
means a crash causes redelivery, which means duplicates, which is why the write
must be **idempotent** (a dedup/merge key on `(series, window)`).

This module owns the batch buffer and the insert; the consume/ack loop that
drives it is wiring in `pipeline.py`.

**The Python-specific trap in here.** `clickhouse_connect`'s async client is a
real async client (aiohttp underneath), so the *network* half of a flush yields
properly and does not block the loop. The half that does block is the part
nobody looks at: building the insert block. Serialising ten thousand rows —
formatting datetimes, encoding the column arrays, compressing — is pure Python
CPU work, and it runs on the event loop, which means every `/ingest` and
`/stream` handler is frozen for its duration. That is the flush latency you will
see in the boss fight, and it gets *worse* as you make batches bigger to please
the column store. Measure it before you decide the batch size, and know where
the escape hatch is (`asyncio.to_thread`, or moving the consumer into its own
process) even if you decide you don't need it.

Scaffold state: the sink is constructed and driven by the pipeline, but push /
flush / query all raise.
"""

from __future__ import annotations

from datetime import datetime

from clickhouse_connect.driver.asyncclient import AsyncClient

from .model import RollupRow

__all__ = ["Sink", "query_range"]

# The column order every insert uses. Kept in one place because the insert is
# positional — a mismatch between this and the table silently writes the sum
# into the min column, and ClickHouse will happily accept it.
COLUMNS = (
    "series_id",
    "measurement",
    "window_start",
    "window_secs",
    "count",
    "sum",
    "min",
    "max",
    "p50",
    "p99",
)


class Sink:
    """Writes batches of rollups to ClickHouse.

    Construct it with a connected client (wired in `main.py`). The batching
    policy — when to flush — is the V3 lesson and lives in `push` / `flush`.
    """

    def __init__(
        self,
        client: AsyncClient,
        table: str,
        batch_max_rows: int,
        batch_max_delay: float,
    ) -> None:
        self._client = client
        self._table = table
        self._batch_max_rows = batch_max_rows
        """Flush when the buffer reaches this many rows."""
        self._batch_max_delay = batch_max_delay
        """Flush at least this often even if the buffer isn't full (the latency
        half of the size-or-time trigger), in seconds."""
        self._buffer: list[RollupRow] = []
        """Pending rollups not yet flushed."""

    async def push(self, rows: list[RollupRow]) -> bool:
        """Add rollups to the batch buffer, flushing if the size trigger is hit.

        Returns whether a flush actually happened, so the caller knows whether it
        may ack the broker.
        """
        # TODO(V3): extend `self._buffer`; if it has reached
        # `self._batch_max_rows`, `await self.flush()` and return True.
        # Otherwise return False — the time trigger in `pipeline.py` will flush
        # it later. Remember: you may only ACK the broker AFTER a successful
        # flush. That ack-after-write ordering is what makes delivery
        # at-least-once instead of at-most-once.
        raise NotImplementedError("V3: buffer rollups; flush on the size trigger")

    async def flush(self) -> None:
        """Write the buffered rollups to ClickHouse and clear the buffer.

        Raises:
            StoreError: the insert failed. The buffer is kept so redelivery can
                retry it — that is the at-least-once contract.
        """
        # TODO(V3): the batched insert — the heart of V3.
        #   - empty buffer: return immediately, and do it before anything else.
        #     The time trigger fires on an idle pipeline too, and a round-trip
        #     per tick is a self-inflicted load.
        #   - build ONE insert for the whole buffer:
        #       await self._client.insert(self._table, data, column_names=COLUMNS)
        #     where `data` is a list of row *sequences* in COLUMNS order. One
        #     round-trip for the batch, never one per row — proving that gap is
        #     a Definition-of-done number.
        #   - on success clear the buffer. On failure KEEP it and raise
        #     `StoreError` (from `.errors`); dropping here would turn a slow
        #     ClickHouse into silent data loss.
        #   - IDEMPOTENCY: the table collapses duplicate `(series_id,
        #     window_start, window_secs)` rows on merge (ReplacingMergeTree), so
        #     a replayed batch doesn't double-count. See migrations/0001_init.sql.
        #   - one flush at a time. Two concurrent flushes would both read and
        #     clear `self._buffer` around an `await`, and the interleaving loses
        #     rows. An `asyncio.Lock` is the obvious fix; convince yourself the
        #     single-consumer design in `pipeline.py` doesn't already give you
        #     this for free before you add one.
        raise NotImplementedError("V3: batch-insert the buffer into ClickHouse, then clear it")

    @property
    def max_delay(self) -> float:
        """The configured time-based flush interval, read by the pipeline loop."""
        return self._batch_max_delay

    @property
    def pending(self) -> int:
        """Rows currently buffered — export as the batch-fill gauge."""
        return len(self._buffer)


async def query_range(
    client: AsyncClient,
    table: str,
    series_id: int,
    start: datetime,
    end: datetime,
) -> list[RollupRow]:
    """Read historical rollups back for `GET /query` — the dashboard's initial
    paint, before the SSE stream takes over.

    A module-level function, not a `Sink` method, because the read path runs in
    the HTTP handlers with its own client handle, independent of the
    pipeline-owned writer that lives in the consumer task.
    """
    # TODO(V3, read path): SELECT the rollups for `series_id` in [start, end)
    # ordered by window_start, and map each result row to a `RollupRow`.
    #   - with a ReplacingMergeTree, use FINAL (or GROUP BY the sort key) so you
    #     read the deduped view rather than the raw duplicates a replay left.
    #   - PARAMETERISE. `client.query(sql, parameters={"series": series_id, …})`
    #     with `{series:UInt64}` placeholders in the SQL. Never f-string a value
    #     into the query — same injection lesson as any other database, and the
    #     fact that this one is "just metrics" is exactly how it gets skipped.
    #   - bound the range server-side too: an unbounded `from`/`to` is a
    #     free full-table scan for any caller (SPEC: security).
    raise NotImplementedError("V3: query a rollup range back out of ClickHouse")
