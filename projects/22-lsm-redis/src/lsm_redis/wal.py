"""V2 — Write-ahead log: durability before the acknowledgement.
`src/lsm_redis/wal.py`.

A memtable (V3) lives in RAM. Acknowledge a `SET`, then get killed before that
key ever reaches a file, and the write is gone. The **write-ahead log** is the
fix and the oldest trick in databases: before a mutation touches the memtable,
append it to a log on disk and — per policy — `fsync`. On restart you **replay**
the log to rebuild the memtable exactly as it was. The rule is in the name: the
log is written *ahead* of the change it describes.

Two things make this more than "append to a file":

1. **Framing + CRC.** Each record is length-delimited and carries a CRC32 over
   its bytes. A crash can leave a *torn* final record — a partial write. Replay
   must detect that (a short read or a bad CRC means "stop here, the rest never
   committed") and recover everything *before* it: no exception, no junk, and
   above all no resurrected write.
2. **The fsync policy.** `fsync` per write is the safe extreme and caps you at
   the disk's sync rate. `SyncPolicy` (see `config.py`) is redis's `appendfsync`
   dial, and the choice *is* the durability-vs-throughput tradeoff. **Group
   commit** — one fsync amortized over many queued writes — is how real engines
   cheat it.

*Concept to internalize:* why durability means "on stable storage before the
ack", what `fsync` actually guarantees (and that a `write` alone guarantees
nothing after a power cut), and how a CRC turns a silent torn tail into a clean
truncation point.

## What Python changes, and it is more than syntax

**`f.flush()` is not durability.** It moves bytes from Python's buffer into the
kernel. They are still in the page cache; the machine losing power loses them.
`os.fsync(f.fileno())` is the call that asks the device. Two different
operations, one of which is 10000x slower than the other, and a WAL that calls
only the first passes every test you can write on a machine you do not power
off.

**A new file's *name* needs a second fsync.** `os.fsync` on the file makes its
*contents* durable; the directory entry that gives it a name is separate
metadata. Create a file, fsync it, lose power, and the file can come back
nameless. `os.open(dir, os.O_RDONLY)` + `os.fsync` on that fd is the fix. This
bites V4 (a freshly flushed SSTable) harder than it bites the WAL, which is
opened once — but it is the same fact, and this is where you learn it.

**fsync blocks, and it blocks the whole process.** This class is deliberately
**synchronous**: it is a file, and pretending otherwise would hide the cost.
`os.fsync` releases the GIL, so it does not freeze other threads — but it
absolutely freezes the event loop if you call it from a coroutine. Every
connection stops being served for the duration, which under `SyncPolicy.ALWAYS`
is milliseconds, per write, at whatever rate you accept writes. Deciding where
the offload happens is the engine's job (`engine.py`), and it is graded.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Self

from .config import SyncPolicy

__all__ = ["Op", "Wal", "WalRecord"]


class Op(StrEnum):
    """The mutation a record carries.

    A delete is *not* a removal — it is an appended **tombstone** that shadows
    older values until compaction (V6) finally drops it. In a log-structured
    store you cannot erase the past; you can only append a newer fact about it.
    """

    SET = "set"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class WalRecord:
    """One logical mutation, as logged and replayed.

    `seq` is the monotonic sequence number that orders writes across the whole
    engine — memtable and every SSTable — so the newest value for a key always
    wins a read. It is assigned by the engine, logged here, and carried into the
    memtable and then into the SSTable: the same number, all the way down.
    """

    seq: int
    op: Op
    key: bytes
    value: bytes | None = None
    """`None` for a delete — a tombstone carries no value."""


class Wal:
    """An append-only durable log.

    Opening the file is plumbing and is wired. `append` and `replay` are V2.
    """

    def __init__(self, file: BinaryIO, path: Path, policy: SyncPolicy) -> None:
        self._file = file
        self.path = path
        self.policy = policy

    @classmethod
    def open(cls, path: Path, policy: SyncPolicy) -> Self:
        """Open (creating if absent) the log for appending.

        `"ab"` rather than `"r+b"` + seek: append mode makes every write go to
        the current end of file at the kernel level, so two writers cannot
        interleave a partial record by racing on the offset. You are not going
        to have two writers — but the failure it prevents is unrecoverable and
        the mode costs nothing.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(open(path, "ab"), path, policy)

    def size_bytes(self) -> int:
        """Current on-disk size. `Engine.open` uses this to decide whether there
        is anything to replay at all — on a fresh log there is not, which is why
        the bare scaffold starts without tripping V2."""
        return self.path.stat().st_size if self.path.exists() else 0

    def append(self, record: WalRecord) -> None:
        """Append one record, honoring the sync policy.

        TODO(V2): frame `record` and write it. You need a length prefix, the
        fields, and a CRC over the frame — `struct.pack` builds the header,
        `zlib.crc32` is the checksum (stdlib; the Rust build pulled `crc32fast`
        only because Rust's std has no CRC). Design the layout so that replay
        can tell "this record is incomplete" from "this record is corrupt"
        without guessing: those are the two different outcomes V2 is graded on.

        Under `SyncPolicy.ALWAYS`, this must not return until the bytes are on
        stable storage — `self._file.flush()` *and* `os.fsync(self._file.fileno())`,
        in that order, or you have implemented `no` with extra steps. Under
        `EVERYSEC` this only writes; a timer calls `sync()`. Under `NO`, neither.

        Group commit is the interesting version and it lives *above* this
        method: a single writer task draining an `asyncio.Queue` of pending
        writes, appending all of them, fsyncing once, then waking every waiter
        (a future per write). One fsync for N acknowledged writes, with the
        durability of N fsyncs. That is a design decision — record which way you
        went, and the measured difference, in `docs/22-design.md`.
        """
        raise NotImplementedError(
            "V2: frame the record with a length + CRC32, write it, honor the policy"
        )

    def sync(self) -> None:
        """Force buffered bytes to stable storage now.

        Called by the `everysec` timer and once more on graceful shutdown. Wired
        — it is the two-line answer to "what does durability actually cost", and
        seeing both calls spelled out is the point.
        """
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        """Flush, sync, and close. Shutdown calls this last."""
        if not self._file.closed:
            self.sync()
            self._file.close()

    @staticmethod
    def replay(path: Path) -> Iterator[WalRecord]:
        """Yield every intact record from the log, in write order.

        TODO(V2): read frames until EOF, verifying each CRC, and yield them.
        Then the part that is actually the vertical:

        * a **short or bad-CRC record at the tail** is the torn write from a
          crash mid-append. Stop cleanly. Everything before it was durably
          committed and must be recovered; the partial record was never
          acknowledged and must not be. No exception — this is the *expected*
          state after a `kill -9`, not an error.
        * a **bad CRC in the middle**, with intact records after it, is real
          corruption (bit rot, a bad disk). Raise `Corrupt`. Silently skipping
          it would serve a hole in the data as if it were the truth.

        Telling those two apart is the whole exercise, and it is why the framing
        you chose in `append` matters: you need to be able to reach the *next*
        record's boundary to know whether anything follows the bad one.

        Returning an iterator rather than a list is deliberate. A WAL is bounded
        by the memtable size in normal operation, but a crash during a long
        flush stall can leave a big one, and `Engine.open` only ever walks it
        once. Streaming it means recovery memory is bounded by the memtable you
        are rebuilding, not by the log you are reading.
        """
        raise NotImplementedError(
            "V2: read + CRC-verify frames, truncating cleanly at the first torn tail"
        )
