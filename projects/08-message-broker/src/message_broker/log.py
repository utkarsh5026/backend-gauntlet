"""V1 — The segmented append-only log: one partition's durable commit log.

This is the layer Kafka would give you. A `Log` is a *directory* of fixed-size
**segment** files. Each segment is named by the base offset it starts at
(`00000000000000000000.log`) and holds records framed as, roughly:

    [len u32][crc u32][timestamp i64][key_len u32][key bytes][value bytes]

Appending writes a frame to the tail of the active segment and returns a
monotonically increasing offset; once the active segment passes `segment_bytes`
it **rolls** to a new one whose base offset is the next offset to be assigned.
Reads never mutate. The exact frame layout is yours to pin down and write into
`docs/08-design.md` — the shape above is a starting point, not a requirement.

The two traps, and the whole point of V1:

  1. **Durability.** A `write()` returning does not mean the bytes are safe: they
     are in the kernel's page cache, and a power cut loses them. The fsync policy
     (per-append vs. batched every N records / T ms) is a deliberate
     throughput-versus-safety dial you *choose and document*, not whatever the OS
     happened to flush.
  2. **Recovery.** After a crash the active segment may end in a half-written
     frame. `open` must scan to the last *complete* frame, set the next offset
     from it, and truncate the torn tail — so a consumer never sees a partial
     record and the next append lands on a clean boundary.

Each segment pairs its `.log` with a sparse `Index` (V2, `index.py`) that turns a
fetch-from-offset into a seek instead of a scan.

**The Python traps, which are not the Rust ones.**

*Every file call here blocks the event loop.* `f.write()`, `f.flush()` and above
all `os.fsync()` are synchronous C calls; an fsync on a busy disk can park the
thread for milliseconds. Do that on the loop and *every* other connection —
fetches, `/healthz`, `/metrics` — stops dead for the duration. That is why
`append` is `async` even though nothing in it awaits a network: the durability
step belongs off the loop, via `asyncio.to_thread` (or one long-lived writer
thread per partition, which doubles as V1's single-writer serialisation). The
GIL is released around the syscall, so the offload is real work getting done in
parallel, not a costume. This is a graded horizontal item — `PYTHONASYNCIODEBUG=1`
will name the handler that blocked.

*Framing is `struct`, and it wants compiling once.* `struct.pack(fmt, ...)`
re-parses the format string on every call. A module-level `struct.Struct(fmt)`
parses it once and gives you `.pack`/`.unpack_from`/`.size`. On the append hot
path that is a measurable difference, and `.unpack_from(buf, offset)` lets you
read a header out of a buffer without slicing a fresh `bytes` first.

*The CRC is stdlib.* `zlib.crc32(data)` is CRC-32 in C — the Rust build only
pulled `crc32fast` because Rust's std has no CRC. Note it returns an unsigned
int already, and that CRC is an *error-detecting* code, not a security check.

*Reading a frame should not copy it twice.* `memoryview(buf)[a:b]` slices without
copying; call `bytes(...)` on it only at the boundary where you hand a value out.

Scaffold state: a `Log` is opened for every partition at startup, but every real
operation raises. The first produce or fetch is your worklist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .index import Index
from .record import Record, StoredRecord

__all__ = ["Log", "LogConfig", "Segment", "base_offset_of"]

# Segment filenames are the base offset zero-padded to 20 digits, so that a plain
# lexicographic sort of the directory is also an ascending numeric sort. 20 is
# the number of digits in 2**64 - 1: the name can never overflow into 21 and
# silently sort wrong.
SEGMENT_NAME_WIDTH = 20


@dataclass(frozen=True, slots=True)
class LogConfig:
    """Tunables shared by every partition's log. Sourced from config in `main`."""

    segment_bytes: int
    """Roll to a new segment once the active one exceeds this many bytes."""

    index_interval_bytes: int
    """Write a sparse index entry roughly every this-many bytes (V2)."""


class Segment:
    """One segment on disk: a `.log` file of framed records plus its sparse
    `.index`.

    `base_offset` is the offset of this segment's first record and is encoded in
    both filenames — which is what lets the read path find the segment holding a
    given offset without opening a single file.
    """

    def __init__(self, base_offset: int, log_path: Path, index: Index) -> None:
        self.base_offset = base_offset
        self.log_path = log_path
        self.index = index
        # TODO(V1): you will want the current write position and an open append
        # handle here, so an append does not reopen + seek-to-end every time.
        # Reopening per append is a syscall pair you pay on every single record.

    @classmethod
    def create(cls, directory: Path, base_offset: int, index_interval_bytes: int) -> Segment:
        """Create a fresh, empty segment starting at `base_offset`.

        Plumbing — it lays down the two files; the framing lives in `Log`.
        """
        stem = f"{base_offset:0{SEGMENT_NAME_WIDTH}d}"
        log_path = directory / f"{stem}.log"
        index = Index.create(directory / f"{stem}.index", index_interval_bytes)
        # Create the (empty) log file so it exists even before the first append.
        log_path.touch(exist_ok=True)
        return cls(base_offset, log_path, index)

    @classmethod
    def open(cls, log_path: Path, index_interval_bytes: int) -> Segment:
        """Open an existing segment, loading its index."""
        base_offset = base_offset_of(log_path) or 0
        index = Index.open(log_path.with_suffix(".index"), index_interval_bytes)
        return cls(base_offset, log_path, index)

    @property
    def size_bytes(self) -> int:
        """Current size of the `.log` on disk — what the roll decision reads."""
        return self.log_path.stat().st_size

    def __repr__(self) -> str:
        return f"Segment(base_offset={self.base_offset}, path={self.log_path.name!r})"


class Log:
    """One partition's append-only log: an ordered list of segments plus the
    offset to assign next.

    Appends target the last (active) segment; reads locate the segment holding
    the wanted offset. Nothing here is thread-safe or task-safe on its own —
    serialising appends is `Partition`'s job (see `partition.py`), and the
    contention model that results is a graded decision.
    """

    def __init__(self, directory: Path, config: LogConfig) -> None:
        self._dir = directory
        self._config = config
        # Sealed + active segments, ascending by base offset. The last is the
        # active (writable) one.
        self._segments: list[Segment] = []
        # The offset the *next* appended record will get, which is also the count
        # of records so far. V1 recovery must restore this from disk on open.
        self._next_offset = 0

    @classmethod
    def open(cls, directory: Path, config: LogConfig) -> Log:
        """Open (creating if needed) the log under `directory`.

        Plumbing sets up the directory. The **recovery** — listing the existing
        segments, validating the tail, and restoring the next offset — is V1
        work, and it is the difference between a broker and a cache.

        TODO(V1 recovery): glob `*.log`, sort by base offset (`base_offset_of`),
        and for the active (last) one scan frames forward to the last *complete*
        record, truncating any torn tail with `os.truncate`, to compute
        `_next_offset`. Starting empty here means a restart currently forgets
        every record — that regression is exactly what your restart test should
        catch first.
        """
        directory.mkdir(parents=True, exist_ok=True)
        return cls(directory, config)

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def config(self) -> LogConfig:
        return self._config

    @property
    def log_end_offset(self) -> int:
        """The offset the next append will be assigned.

        Also the log-end offset a consumer is chasing: consumer lag (a graded
        metric) is this minus the group's committed offset.
        """
        return self._next_offset

    @property
    def segment_count(self) -> int:
        """How many segments back this log — the observable proof that rolling
        happens at all."""
        return len(self._segments)

    async def append(self, record: Record) -> int:
        """Append a record, returning the offset it was assigned. The core of V1.

        TODO(V1): the append path —
          * if there is no active segment, or the active one is past
            `config.segment_bytes`, roll: `Segment.create` with
            `base_offset = self._next_offset` and append it to `_segments`;
          * frame the record (length, CRC over the rest, timestamp, key length,
            key, value) and write it at the tail of the active segment;
          * tell the segment's sparse index about the frame
            (`index.maybe_index(...)`, V2) so it can decide whether to record it;
          * apply the chosen fsync policy — and this is the part that must not
            run on the event loop (`asyncio.to_thread(os.fsync, fd)`);
          * assign `_next_offset`, increment it, and return the offset assigned.

        Note the ordering that durability demands: the offset is only real once
        the bytes are. Returning an offset for a record that a crash would lose
        is the lie this whole vertical exists to prevent.
        """
        raise NotImplementedError(
            "V1: frame + durably append a record, rolling the segment if full"
        )

    async def read_from(self, offset: int, max_records: int) -> list[StoredRecord]:
        """Read up to `max_records` records starting at `offset`.

        Returns an empty list — **not** an error, and never a hang — when
        `offset` is at or past the log end. That is the tailing consumer, the
        single most common case in a healthy broker, and treating it as an error
        would make every well-behaved client look broken.

        TODO(V1 + V2): locate the segment with the largest `base_offset <=
        offset` (`bisect` over the segment base offsets, same trick as the
        index), then use that segment's sparse index to seek near `offset` and
        scan forward, decoding and CRC-checking each frame until `max_records`
        are collected or the log ends. A frame that fails its check raises
        `CorruptFrame` — it is never returned as data.

        Stream it: read the frames you need, not the segment. Buffering a 64 MiB
        segment into RAM to answer a 10-record fetch is a graded failure
        (horizontal: "reads stream from the segment file").
        """
        raise NotImplementedError("V1/V2: seek to `offset` via the sparse index and read a batch")

    async def flush(self) -> None:
        """Durably flush the active segment.

        Called on graceful shutdown (see `main.py`) so a clean stop leaves no
        torn tail and loses no acknowledged write. Under a batched fsync policy
        this is the last batch; under per-append it is a no-op — and knowing
        which is which for *your* policy is the point.

        TODO(V1): flush + fsync the active segment's file handle (off the loop),
        and its directory entry too if you created a segment this run.
        """
        raise NotImplementedError("V1: flush + fsync the active segment")


def base_offset_of(path: Path) -> int | None:
    """Parse the base offset a segment file encodes in its name.

    `00000000000000000042.log` -> 42. Returns `None` for anything that is not a
    segment name, so recovery can skip stray files instead of crashing on them.
    """
    try:
        return int(path.stem)
    except ValueError:
        return None
