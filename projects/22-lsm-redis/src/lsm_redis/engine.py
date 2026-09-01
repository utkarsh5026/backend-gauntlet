"""The LSM tree: where the verticals compose into one key/value store.

This is the orchestrator the RESP command layer (`server.py`) and the HTTP
sidecar (`routes.py`) call. It owns the write path (WAL -> memtable -> flush) and
the read path (memtable -> frozen memtables -> SSTables, reconciled by recency),
and it holds the block cache and the sequence counter that orders every write.

`Engine.open` is **fully wired**: on a fresh data directory it builds an empty,
serving engine, so the bare scaffold runs. The interesting methods — `get`,
`set`, `delete`, `flush_memtable`, `run_compaction` — are where the verticals
meet, and they raise.

## Where the blocking work goes — the decision this module exists to force

Every other project in the gauntlet could keep its I/O in a library that was
already async. This one cannot: `os.pread`, `os.write` and `os.fsync` are
synchronous calls, and there is no async filesystem API in CPython that is not
a thread pool wearing a costume. So the "no blocking call on the event loop"
checklist item stops being hygiene here and becomes the central design question.

The shape of the problem, honestly stated:

* **`os.fsync` under `SyncPolicy.ALWAYS`** is milliseconds, on the write path,
  per write. On the loop, that is milliseconds during which *no* connection is
  served — not just the writer's. At 20000 writes/sec (the boss fight's floor)
  it is not a tail-latency problem, it is an arithmetic impossibility.
* **`os.pread` for a block** is tens of microseconds warm from the page cache
  and milliseconds cold. The GIL is released for the syscall, so a thread pool
  genuinely overlaps these.
* **Decoding and searching a block** is Python bytecode. It holds the GIL, and
  no pool makes it disappear — only doing less of it does (which is what the
  block cache is for).

Which means the interesting choices are:

1. **`asyncio.to_thread` per operation.** Simplest, and each call costs a
   thread-pool round trip — measurable at these rates. Note that the default
   executor is unbounded-ish (`min(32, cpu_count + 4)` threads) and shared with
   everything else; a bounded `ThreadPoolExecutor` you own is the "bounded pool
   sized on purpose" checklist item, and its size wants tuning *together* with
   how many connections you accept.
2. **A single writer task** draining an `asyncio.Queue` of pending writes,
   which serializes the WAL for free and is the natural home for group commit
   (one fsync, N acknowledged writes — see `wal.py`). Reads stay concurrent.
3. **Both**: a writer task for the WAL, a small read pool for block reads.

Whatever you choose, `PYTHONASYNCIODEBUG=1` will tell you when you got it wrong
— it logs any callback that occupies the loop for more than 100 ms — and the
reasoning belongs in `docs/22-design.md`.

`Engine.open` deliberately blocks: it runs during startup, before the listener
exists, so there is nothing to starve. Recognizing *that* distinction — blocking
is about the serving path, not about the process — is worth as much as the rule
itself.
"""

from __future__ import annotations

from typing import Self

import structlog
from pydantic import BaseModel

from .block_cache import BlockCache
from .config import Settings
from .memtable import TOMBSTONE, Memtable
from .sstable import SSTable
from .wal import Wal

__all__ = ["Engine", "EngineStats"]

logger = structlog.get_logger(__name__)

SSTABLE_SUFFIX = ".sst"


class EngineStats(BaseModel):
    """A snapshot of engine internals, for `/stats` and the metrics gauges.

    These are the numbers the observability checklist asks to *move* in the
    right direction: memtable bytes rise on writes and drop on flush, the
    SSTable count rises on flush and falls on compaction. A stats endpoint that
    reports plausible constants is worse than none — it is a dashboard that lies
    during the incident it was built for.
    """

    keys_memtable: int
    memtable_bytes: int
    immutable_memtables: int
    sstables: int
    block_cache_capacity_bytes: int
    block_cache_used_bytes: int
    block_cache_hits: int
    block_cache_misses: int
    sequence: int


class Engine:
    """The LSM tree.

    Construct with `Engine.open`, not directly — recovery is part of what makes
    an engine valid, and an `Engine` that skipped it would be an engine that
    silently lost the last second of writes.
    """

    def __init__(
        self, settings: Settings, wal: Wal, memtable: Memtable, sstables: list[SSTable]
    ) -> None:
        self.settings = settings
        self.wal = wal
        """The durable log. Every mutation goes here before it goes anywhere
        else — that ordering is the entire meaning of "write-ahead"."""

        self.memtable = memtable
        """The active in-memory write buffer (V3)."""

        self.immutable: list[Memtable] = []
        """Frozen memtables awaiting a flush to an SSTable (V4). Read *after*
        the active memtable and *before* the SSTables — they hold the newest
        value for their keys until their file exists, so skipping them during a
        flush is a window in which recent writes read as missing."""

        self.sstables = sstables
        """On-disk sorted runs, newest first. V6 organizes these into levels."""

        self.block_cache = BlockCache(settings.block_cache_bytes)

        self._seq = max((t.id for t in sstables), default=0)
        self._next_sstable_id = self._seq + 1

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls, settings: Settings) -> Self:
        """Open (or recover) the store rooted at `settings.data_dir`.

        Wired end to end: creates the directory, opens the WAL, replays it
        **only if it is non-empty** (a fresh log needs no V2 replay, which is
        why the bare scaffold starts), discovers existing SSTable files, and
        builds the block cache.

        Recovery order is not arbitrary — SSTables first, then the WAL on top.
        The WAL holds exactly the writes that had not reached a file yet, so
        replaying it last is what makes them win.
        """
        settings.data_dir.mkdir(parents=True, exist_ok=True)

        # Discover existing SSTables, oldest id first so `sstables` ends up
        # newest-first after the reverse below. `NNNNNN.sst`, zero-padded, so
        # lexical order matches numeric order on disk too.
        tables: list[SSTable] = []
        for path in sorted(settings.data_dir.glob(f"*{SSTABLE_SUFFIX}")):
            try:
                table_id = int(path.stem)
            except ValueError:
                logger.warning("ignoring unrecognized file in data_dir", path=str(path))
                continue
            tables.append(SSTable.open(path, table_id))
        tables.reverse()

        wal = Wal.open(settings.wal_path, settings.wal_sync)
        memtable = Memtable()
        recovered = 0
        if wal.size_bytes() > 0:
            for record in Wal.replay(settings.wal_path):
                memtable.insert(
                    record.key, record.value if record.value is not None else TOMBSTONE, record.seq
                )
                recovered += 1

        engine = cls(settings, wal, memtable, tables)
        logger.info(
            "engine opened",
            data_dir=str(settings.data_dir),
            sstables=len(tables),
            wal_records_replayed=recovered,
            memtable_keys=len(memtable),
        )
        return engine

    def next_seq(self) -> int:
        """The next monotonic sequence number for a write.

        Safe without a lock *because* there is no `await` in it: the read, the
        add and the store run as one uninterrupted stretch of bytecode, so no
        other coroutine can observe or duplicate a value. Add an `await`
        anywhere in this method and that stops being true — which is a good
        thing to know before you decide to make the WAL append happen here.
        """
        self._seq += 1
        return self._seq

    def next_sstable_id(self) -> int:
        """Allocate the next SSTable file id. Same no-`await` reasoning."""
        table_id = self._next_sstable_id
        self._next_sstable_id += 1
        return table_id

    # -- the read path -----------------------------------------------------

    async def get(self, key: bytes) -> bytes | None:
        """`GET key` — the newest value for `key`, or `None` if absent or
        deleted.

        TODO(read path — V3 -> V4 -> V7): reconcile across levels
        **newest-first**: the active memtable, then each frozen memtable
        (newest first), then SSTables newest to oldest. The *first* level with
        an opinion wins — and a tombstone is an opinion. It means "deleted", so
        return `None` and **stop**; do not fall through to an older SSTable that
        still holds the key.

        That single rule is most of the correctness of an LSM read, and getting
        it wrong does not look like a bug at first: it looks like a key you
        deleted last week reappearing after a compaction, months later, in
        production.

        SSTable lookups go through the bloom (V5) and the block cache (V7) — so
        the cheap checks run before the expensive ones, and a miss on a key that
        is in no file costs you N in-memory bit probes rather than N disk reads.

        See the module docstring for where the disk work runs; a `get` that
        `os.pread`s on the event loop will pass every test you write and fail
        the boss fight.
        """
        raise NotImplementedError(
            "read path: memtable -> frozen -> SSTables (newest wins, tombstone stops the search)"
        )

    # -- the write path ----------------------------------------------------

    async def set(self, key: bytes, value: bytes) -> None:
        """`SET key value` — record the write durably, then buffer it.

        TODO(write path — V2 -> V3): stamp the write with `next_seq()`, append
        it to the WAL (V2), and only **after** the sync policy is satisfied
        insert it into the active memtable (V3) and return. The caller replies
        `+OK` the instant this returns, so returning early is not an
        optimization, it is a lie about durability.

        Then the rotation: if the memtable `is_full(memtable_max_bytes)`, freeze
        it — move it to `self.immutable`, install a fresh `Memtable`, start a
        fresh WAL segment — and hand the frozen one to `flush_memtable` in the
        background. Freezing rather than blocking is what keeps writes flowing
        during a flush, and it is the difference between a pause and a stall.

        The failure to design for: if flushes cannot keep up, `self.immutable`
        grows without bound and you have moved the write stall into RAM instead
        of preventing it. Real engines slow writers down deliberately at that
        point (RocksDB literally calls it a *write stall*). Deciding what yours
        does when the queue is deep — and *measuring* it under the boss fight —
        is the whole vertical.
        """
        raise NotImplementedError(
            "write path: WAL append (V2) -> memtable insert (V3) -> maybe freeze + flush"
        )

    async def delete(self, key: bytes) -> bool:
        """`DEL key` — returns whether the key existed. A delete is a
        **tombstone write**, not an erase.

        TODO(write path — V2 -> V3): the same shape as `set`, appending a
        delete record at a fresh seq and inserting `TOMBSTONE` into the
        memtable.

        The return value is the interesting part, and it is a real cost
        decision: redis's `DEL` replies with how many keys it actually removed,
        which means knowing whether the key was live — which means a full read
        (memtable, then every SSTable, through blooms and blocks) *before* the
        write. That turns an O(1) append into a read-modify-write on the write
        path, and under the boss fight's firehose it is the difference between
        keeping up and not.

        Your options are: pay for it, answer from the memtable only and be
        wrong about keys that live on disk, or always report `1`. Real
        Redis-compatible stores built on LSMs make exactly this compromise and
        document it. Pick one and write down which, and why, in
        `docs/22-design.md`.
        """
        raise NotImplementedError(
            "write path: append a tombstone (V2) + insert it into the memtable (V3)"
        )

    async def flush_memtable(self) -> None:
        """Flush the oldest frozen memtable to a new SSTable, then retire its
        WAL segment.

        TODO(V4): take the oldest entry from `self.immutable`, allocate an id
        with `next_sstable_id()`, call `SSTable.create` over its
        `items_sorted()`, publish the new table at the front of `self.sstables`,
        drop the frozen memtable, and **only then** delete the WAL segment that
        covered it.

        That ordering is the whole method. Retire the WAL before the SSTable is
        durable and a crash in between loses every write in it — writes that
        were acknowledged, which is the one thing this engine promised never to
        do. The rule generalizes: the old copy of data is deleted last, always,
        and "durable" means fsynced, including the directory entry (see
        `sstable.py`).
        """
        raise NotImplementedError(
            "V4: write the frozen memtable to an SSTable, publish it, then retire its WAL"
        )

    async def run_compaction(self) -> bool:
        """Compact SSTables to bound read and space amplification (V6). Returns
        whether any work was done.

        Called on a timer by `compaction.compaction_loop` when
        `RUN_COMPACTION=true`.

        TODO(V6): ask `compaction.plan` whether there is work; if so, merge the
        inputs with `compaction.merge_sorted_runs` into one or more new tables
        via `SSTable.create`, then **install the outputs and remove the inputs
        atomically from a reader's point of view** — swap `self.sstables` to a
        new list in one assignment rather than mutating it in place. A reader
        that sees the inputs gone before the outputs are in place gets a miss on
        a key that exists, which is a correctness bug you will find as a flaky
        test and misdiagnose as a race in the harness.

        Delete the input files only after the outputs are durable and published,
        and invalidate their entries in the block cache — the ids are gone, so
        anything cached under them is unreachable garbage occupying the byte
        budget that live blocks need.

        See the module docstring in `compaction.py` for where this work runs.
        On CPython that decision is the difference between passing and failing
        the boss fight's "throughput does not collapse" criterion.
        """
        raise NotImplementedError(
            "V6: plan + merge SSTables, drop shadowed keys/tombstones, swap outputs in"
        )

    # -- observation + shutdown -------------------------------------------

    def stats(self) -> EngineStats:
        """A snapshot of engine internals. Wired — it powers `/stats` and the
        metrics gauges on the bare scaffold, so you can watch the memtable fill
        and the SSTable count climb while you build the store.

        Synchronous and allocation-light on purpose: it is scraped on a timer by
        Prometheus and polled by humans during the boss fight, and an
        observability path that costs anything measurable is one you will be
        tempted to turn off exactly when you need it.
        """
        cache = self.block_cache.stats()
        return EngineStats(
            keys_memtable=len(self.memtable),
            memtable_bytes=self.memtable.approx_bytes,
            immutable_memtables=len(self.immutable),
            sstables=len(self.sstables),
            block_cache_capacity_bytes=self.block_cache.capacity_bytes,
            block_cache_used_bytes=self.block_cache.used_bytes,
            block_cache_hits=cache.hits,
            block_cache_misses=cache.misses,
            sequence=self._seq,
        )

    async def close(self) -> None:
        """Flush the WAL and close it. Called from the lifespan's `finally`,
        after in-flight commands have drained.

        Wired, and it is only half of the graceful-shutdown criterion: this
        makes a *clean* stop lose nothing. A dirty stop — `kill -9`, a power cut
        — is what WAL replay (V2) is for, and the boss fight tests exactly that.
        A store that only survives polite shutdowns has not solved durability,
        it has avoided testing it.
        """
        self.wal.close()
