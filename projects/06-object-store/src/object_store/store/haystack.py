"""Haystack-style small-object packing — the append-only volume layout.

Thousands of tiny objects occupy a handful of append-only **volume** files
instead of one inode each, and an in-memory map locates every needle by
`digest → (volume, payload offset, size)`. Identity is unchanged: the digest is
still the plaintext SHA-256, and volumes are anonymous containers.

## Why this exists

FileCas costs one inode, one directory entry and one `open` syscall per object.
For a 400-byte thumbnail that overhead dwarfs the payload, and at a few hundred
million of them the filesystem's metadata — not the data — is what fills the
disk and what a cold `readdir` has to walk. Packing turns N opens into one open
plus a seek, which is the entire trick Facebook's Haystack paper is about.

## On-disk needle

```text
[ digest hex: 64 bytes ][ size: u64 little-endian ][ payload: size bytes ]
```

`NeedleLocator.payload_offset` points at the **payload**, past the header, so a
GET seeks straight to the bytes without re-parsing anything. The digest is
repeated in the header purely for recovery: it is what lets a volume scan rebuild
the index when the index is gone.

## Durable live index

Mutations append one NDJSON record to `volumes/needles.log` (fsynced) and update
the RAM map. A checkpoint rewrites `volumes/needles.json` from RAM and truncates
the log. The split is the usual one: **the log is the durability path, the JSON
is a fast-boot snapshot**. Boot loads the snapshot, then replays the log over
it; a full volume scan is recovery only, for when both are missing.

Deletes are tombstones — the bytes stay until compaction, because you cannot
punch a hole in the middle of an append-only file and keep every offset after it
valid. Compaction copies live needles into a **new** volume id and unlinks the
old file: closed volumes are never rewritten in place, so a crash mid-compaction
can only ever leave a stray temp, never a half-rewritten volume that readers are
still holding offsets into.

## Threading

Every method here is synchronous and internally locked; `Store` calls them via
`asyncio.to_thread`. Two locks, always taken in this order where both are
needed: `_active_lock` (the append path) then `_index_lock` (the map). The WAL
has its own lock nested inside neither, and `checkpoint` holds it across the
snapshot+truncate so no append can slip between the two and be lost.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import structlog

from ..durable import TempEntry, atomic_write_sibling, publish_temp
from ..errors import IntegrityError, InvalidRequest, NoSuchKey
from ..objects import Digest

__all__ = [
    "Haystack",
    "NeedleLocator",
    "VolumeId",
    "NEEDLE_HEADER_LEN",
]

logger = structlog.get_logger(__name__)

VOLUME_EXT = ".dat"
INDEX_FILE = "needles.json"
LOG_FILE = "needles.log"

INDEX_VERSION = 3
"""Bumped when the snapshot shape changes. Version 2 was the Rust layout; this
is the same field set written by the Python port, and refusing to load an older
version beats guessing at a migration for a lab feature."""

_SIZE_STRUCT = struct.Struct("<Q")
"""Little-endian u64 — the needle header's payload length."""

NEEDLE_HEADER_LEN = Digest.LEN + _SIZE_STRUCT.size
"""64 hex digest characters + 8 length bytes = 72."""

VolumeId = str
"""A volume's identity: the UUID string in `volumes/<uuid>.dat`.

UUIDs rather than a counter so allocation is a local decision — no `readdir` to
find max+1, and no coordination between a compaction minting a new volume and a
concurrent PUT sealing the old one."""


def _new_volume_id() -> VolumeId:
    return str(uuid.uuid4())


def _parse_volume_id(name: str) -> VolumeId | None:
    """`<uuid>.dat` → the id, or `None` for anything else in the directory."""
    if not name.endswith(VOLUME_EXT):
        return None
    stem = name[: -len(VOLUME_EXT)]
    try:
        return str(uuid.UUID(stem))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class NeedleLocator:
    """Where a needle's bytes sit inside a volume file."""

    volume_id: VolumeId
    payload_offset: int
    """Byte offset of the **payload** start, past the header."""
    size: int
    """Payload length in bytes, excluding the header."""


@dataclass(slots=True)
class NeedleRecord:
    """One index row: a locator plus its tombstone / quarantine flags."""

    locator: NeedleLocator
    deleted: bool = False
    quarantined: bool = False

    @property
    def is_live(self) -> bool:
        """Whether a GET may be served from this needle."""
        return not self.deleted and not self.quarantined


@dataclass(slots=True)
class _ActiveFile:
    """The current append target: an open handle and the next write offset."""

    volume_id: VolumeId
    handle: BinaryIO
    next_offset: int


class Haystack:
    """Append-only volume packing for small content-addressed objects."""

    def __init__(self, root: Path, max_volume_size: int) -> None:
        self.volumes_dir = root / "volumes"
        self.volumes_dir.mkdir(parents=True, exist_ok=True)
        # A volume must fit at least one byte of payload past the header, or
        # `fits_in_volume` is false for everything and the layout silently
        # degrades to FileCas with extra steps.
        self.max_volume_size = max(max_volume_size, NEEDLE_HEADER_LEN + 1)

        self._index: dict[Digest, NeedleRecord] = {}
        self._index_lock = threading.RLock()
        self._wal_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active: _ActiveFile | None = None
        self._dirty = False

        self._bootstrap_index()

    # ── paths ───────────────────────────────────────────────────────────────

    def volume_path(self, volume_id: VolumeId) -> Path:
        return self.volumes_dir / f"{volume_id}{VOLUME_EXT}"

    @property
    def index_path(self) -> Path:
        return self.volumes_dir / INDEX_FILE

    @property
    def log_path(self) -> Path:
        return self.volumes_dir / LOG_FILE

    # ── boot ────────────────────────────────────────────────────────────────

    def _bootstrap_index(self) -> None:
        """Snapshot → log replay, or a volume scan when both are missing."""
        index = self._load_snapshot()
        self._replay_log_into(index)

        if not index and self._any_volume_files():
            logger.warning("haystack index empty but volumes present; recovering via volume scan")
            index = self._rebuild_from_volumes()
            with self._index_lock:
                self._index = index
            self.checkpoint()
            return

        with self._index_lock:
            self._index = index
        # Leave a snapshot behind even for a fresh empty store, so tooling and
        # the next boot never have to distinguish "no index yet" from "index
        # lost".
        if not self.index_path.is_file():
            self.checkpoint()

    def _any_volume_files(self) -> bool:
        return any(_parse_volume_id(entry.name) is not None for entry in self.volumes_dir.iterdir())

    def _load_snapshot(self) -> dict[Digest, NeedleRecord]:
        """Deserialise `needles.json`. A missing or unreadable file is empty.

        Unreadable is a warning rather than a hard failure on purpose: the WAL
        replay that follows can reconstruct the same state, and refusing to boot
        over a corrupt *cache* would turn a recoverable situation into an
        outage.
        """
        try:
            raw = self.index_path.read_bytes()
        except FileNotFoundError:
            return {}

        try:
            payload: Any = json.loads(raw)
            if payload.get("version") != INDEX_VERSION:
                raise ValueError(
                    f"unsupported needles.json version {payload.get('version')!r} "
                    f"(want {INDEX_VERSION})"
                )
            index: dict[Digest, NeedleRecord] = {}
            for entry in payload["entries"]:
                index[Digest(entry["digest"])] = NeedleRecord(
                    locator=NeedleLocator(
                        volume_id=entry["volume_id"],
                        payload_offset=entry["offset"],
                        size=entry["size"],
                    ),
                    deleted=entry.get("deleted", False),
                    quarantined=entry.get("quarantined", False),
                )
            return index
        except (ValueError, KeyError, TypeError) as err:
            logger.warning(
                "haystack needles.json unreadable; falling back to log replay",
                error=str(err),
            )
            return {}

    def _replay_log_into(self, index: dict[Digest, NeedleRecord]) -> None:
        """Apply every complete NDJSON line from `needles.log` onto `index`.

        Stops at the first unreadable line rather than skipping it. A torn
        trailing append is the expected shape of a crash, and everything after a
        torn record is unordered garbage — but everything *before* it is valid,
        so the earlier ops must survive.
        """
        if not self.log_path.is_file():
            return
        with self.log_path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    op: Any = json.loads(stripped)
                    self._apply_log_op(op, index)
                except (ValueError, KeyError, TypeError) as err:
                    logger.warning(
                        "haystack needles.log: stopping replay at unreadable line",
                        line=line_no,
                        error=str(err),
                    )
                    return

    @staticmethod
    def _apply_log_op(op: Any, index: dict[Digest, NeedleRecord]) -> None:
        kind = op["op"]
        digest = Digest(op["digest"])
        if kind == "put":
            index[digest] = NeedleRecord(NeedleLocator(op["volume_id"], op["offset"], op["size"]))
        elif kind == "delete":
            if (record := index.get(digest)) is not None:
                record.deleted = True
        elif kind == "quarantine":
            if (record := index.get(digest)) is not None:
                record.quarantined = True
        else:
            raise ValueError(f"unknown needles.log op {kind!r}")

    # ── durability ──────────────────────────────────────────────────────────

    def _append_wal(self, op: dict[str, Any]) -> None:
        """Append one NDJSON op and fsync it.

        The fsync is the whole point: this record is what makes a committed
        needle findable after a crash, so returning before it is on the platter
        would mean acknowledging a PUT whose index entry can vanish.
        """
        line = (json.dumps(op) + "\n").encode("utf-8")
        with self._wal_lock:
            with self.log_path.open("ab") as handle:
                handle.write(line)
                handle.flush()

                os.fsync(handle.fileno())
            self._dirty = True

    def checkpoint(self) -> None:
        """Rewrite `needles.json` from RAM and truncate `needles.log`.

        Holds the WAL lock across both halves so an append cannot land between
        the snapshot and the truncate — that record would be in neither file and
        the needle it describes would be unreachable.
        """
        with self._wal_lock:
            with self._index_lock:
                entries = [
                    {
                        "digest": str(digest),
                        "volume_id": record.locator.volume_id,
                        "offset": record.locator.payload_offset,
                        "size": record.locator.size,
                        "deleted": record.deleted,
                        "quarantined": record.quarantined,
                    }
                    for digest, record in self._index.items()
                ]
            payload = json.dumps({"version": INDEX_VERSION, "entries": entries}, indent=2)
            atomic_write_sibling(self.index_path, payload.encode("utf-8"))
            with self.log_path.open("wb"):
                pass
            self._dirty = False

    @property
    def is_index_dirty(self) -> bool:
        """Whether the WAL holds ops no snapshot covers yet."""
        with self._wal_lock:
            return self._dirty

    # ── geometry ────────────────────────────────────────────────────────────

    @staticmethod
    def needle_len(payload_len: int) -> int:
        """Framed length on disk: header + payload."""
        return NEEDLE_HEADER_LEN + payload_len

    def fits_in_volume(self, payload_len: int) -> bool:
        """Whether a payload of this size can be packed at all."""
        return self.needle_len(payload_len) <= self.max_volume_size

    def indexed_digests(self) -> list[Digest]:
        """Digests currently servable — not deleted, not quarantined."""
        with self._index_lock:
            return [d for d, record in self._index.items() if record.is_live]

    def contains(self, digest: Digest) -> bool:
        with self._index_lock:
            record = self._index.get(digest)
            return record is not None and record.is_live

    def locate(self, digest: Digest) -> NeedleLocator | None:
        with self._index_lock:
            record = self._index.get(digest)
            return record.locator if record is not None and record.is_live else None

    def scan_occupancy(self) -> tuple[int, int]:
        """`(volume_file_count, total_bytes_on_disk)` for the boot gauges.

        Counts only `*.dat` — `needles.json` and the WAL are metadata, not
        stored object bytes, and folding them in would make the gauge lie.
        """
        count = 0
        total = 0
        for entry in self.volumes_dir.iterdir():
            if _parse_volume_id(entry.name) is None or not entry.is_file():
                continue
            count += 1
            total += entry.stat().st_size
        return count, total

    # ── write path ──────────────────────────────────────────────────────────

    def _ensure_active(self, size_to_append: int) -> _ActiveFile:
        """Return an open volume with room for `size_to_append` bytes.

        Closed volumes are **immutable**: once the active handle is dropped —
        because the volume filled up, or compaction sealed it — that `.dat` is
        never reopened for append. A new write always creates the next volume
        id, which is what lets compaction copy from a retired file without
        racing an appender.
        """
        active = self._active
        if active is not None and active.next_offset + size_to_append <= self.max_volume_size:
            return active

        if active is not None:
            active.handle.close()

        volume_id = _new_volume_id()
        # `xb` (create-exclusive) so a UUID collision or a stale file becomes an
        # error rather than a silent append into someone else's volume.
        handle = self.volume_path(volume_id).open("xb+")
        self._active = _ActiveFile(volume_id, handle, 0)
        return self._active

    def commit_temp(self, temp: Path, digest: Digest) -> None:
        """Append `temp`'s bytes as a framed needle and index the digest.

        Order is volume → WAL → RAM, and it is the same blob-then-pointer rule
        V3 applies one level up: the payload is fsynced into the volume *before*
        the record that makes it findable, so a crash between the two leaves
        unreferenced bytes (reclaimed by the next compaction), never an index
        row pointing at an offset that holds nothing.
        """
        payload_size = temp.stat().st_size
        framed = self.needle_len(payload_size)

        with self._active_lock:
            active = self._ensure_active(framed)
            needle_start = active.next_offset
            locator = NeedleLocator(
                volume_id=active.volume_id,
                payload_offset=needle_start + NEEDLE_HEADER_LEN,
                size=payload_size,
            )

            active.handle.seek(needle_start)
            active.handle.write(str(digest).encode("ascii"))
            active.handle.write(_SIZE_STRUCT.pack(payload_size))
            with temp.open("rb") as source:
                _copy_stream(source, active.handle, payload_size)
            active.handle.flush()

            os.fsync(active.handle.fileno())
            active.next_offset = needle_start + framed

        self._append_wal(
            {
                "op": "put",
                "digest": str(digest),
                "volume_id": locator.volume_id,
                "offset": locator.payload_offset,
                "size": locator.size,
            }
        )
        with self._index_lock:
            self._index[digest] = NeedleRecord(locator)

        temp.unlink(missing_ok=True)

    # ── read path ───────────────────────────────────────────────────────────

    def _require_readable(self, digest: Digest) -> NeedleLocator:
        with self._index_lock:
            record = self._index.get(digest)
        if record is None or record.deleted:
            raise NoSuchKey()
        if record.quarantined:
            raise IntegrityError()
        return record.locator

    def open_blob(self, digest: Digest) -> NeedleReader:
        """Open a needle's payload, bounded to exactly its size.

        The bound matters: a volume is one long file, so an unbounded read from
        a needle's offset would run straight on into the next object's bytes.
        """
        locator = self._require_readable(digest)
        return self.open_blob_range(digest, 0, locator.size - 1)

    def open_blob_range(self, digest: Digest, start: int, end: int) -> NeedleReader:
        """Open an inclusive byte range within a needle's payload."""
        locator = self._require_readable(digest)
        if start > end or end >= locator.size:
            raise InvalidRequest(
                f"invalid range: start={start} end={end} needle_size={locator.size}"
            )
        handle = self.volume_path(locator.volume_id).open("rb")
        handle.seek(locator.payload_offset + start)
        return NeedleReader(handle, end - start + 1)

    # ── deletion and integrity ──────────────────────────────────────────────

    def remove(self, digest: Digest) -> int | None:
        """Tombstone a digest; the bytes go at compaction. Returns its size.

        Idempotent — a missing or already-deleted digest returns `None`.
        """
        with self._index_lock:
            record = self._index.get(digest)
            size = record.locator.size if record is not None and not record.deleted else None
        if size is None:
            return None

        self._append_wal({"op": "delete", "digest": str(digest)})
        with self._index_lock:
            if (record := self._index.get(digest)) is not None:
                record.deleted = True
        return size

    def mark_quarantined(self, digest: Digest) -> bool:
        """Flag a digest as failing integrity; GET will refuse it from now on."""
        with self._index_lock:
            record = self._index.get(digest)
            should = record is not None and not record.quarantined
        if not should:
            return False

        self._append_wal({"op": "quarantine", "digest": str(digest)})
        with self._index_lock:
            if (record := self._index.get(digest)) is not None:
                record.quarantined = True
        return True

    def scrub_once(self) -> int:
        """Re-hash every live needle; quarantine any whose payload ≠ its name.

        Walks the **durable index**, not the volume files: a volume scan cannot
        see tombstones or quarantine flags (they live only in the index), so it
        would happily re-verify bytes that are logically gone and report them as
        healthy. Grouping by volume means one open per file rather than one per
        needle, which is the entire reason packing is worth doing.

        Returns how many needles were examined.
        """
        by_volume: dict[VolumeId, list[tuple[Digest, NeedleLocator]]] = {}
        with self._index_lock:
            for digest, record in self._index.items():
                if record.is_live:
                    by_volume.setdefault(record.locator.volume_id, []).append(
                        (digest, record.locator)
                    )

        examined = 0
        for volume_id, needles in by_volume.items():
            try:
                handle = self.volume_path(volume_id).open("rb")
            except FileNotFoundError:
                # The volume was compacted away between the snapshot and now;
                # the next pass will see the remapped locators.
                continue
            with handle:
                for digest, locator in needles:
                    examined += 1
                    handle.seek(locator.payload_offset)
                    hasher = hashlib.sha256()
                    remaining = locator.size
                    short_read = False
                    while remaining > 0:
                        chunk = handle.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            short_read = True
                            break
                        hasher.update(chunk)
                        remaining -= len(chunk)
                    if short_read or hasher.hexdigest() != digest:
                        logger.warning(
                            "haystack scrub detected a needle mismatch; quarantining",
                            digest=str(digest),
                        )
                        self.mark_quarantined(digest)
        return examined

    # ── compaction ──────────────────────────────────────────────────────────

    def compaction(self) -> list[Digest]:
        """Rewrite volumes holding dead needles into fresh ones.

        Per dirty volume: seal it if it is the append target, snapshot its live
        needles, stage a copy under a sibling temp, fsync, publish it under a
        **new** volume id, remap the index, checkpoint, then unlink the old
        file. Peak disk is roughly old + live, which is the price of never
        rewriting a file readers may be mid-read of.

        Returns the digests whose rows were dropped. A caller keeping a separate
        routing map must remove **only these keys** and never rebuild its map
        wholesale — a full rebuild races a concurrent commit and can clobber an
        insert that happened during the copy.
        """
        with self._index_lock:
            dirty = {
                record.locator.volume_id for record in self._index.values() if not record.is_live
            }

        dropped: list[Digest] = []
        for old_id in dirty:
            dropped.extend(self._compact_volume(old_id))
        return dropped

    def _compact_volume(self, old_id: VolumeId) -> list[Digest]:
        # Seal first so no PUT can append into the volume being retired. Held
        # only for the seal — the copy below must not run under the append lock.
        with self._active_lock:
            if self._active is not None and self._active.volume_id == old_id:
                self._active.handle.close()
                self._active = None

        with self._index_lock:
            live = [
                (digest, record.locator)
                for digest, record in self._index.items()
                if record.locator.volume_id == old_id and record.is_live
            ]

        old_path = self.volume_path(old_id)

        if not live:
            dropped = self._drop_volume_rows(old_id)
            self.checkpoint()
            old_path.unlink(missing_ok=True)
            return dropped

        new_id = _new_volume_id()
        new_locators: list[tuple[Digest, NeedleLocator]] = []
        with TempEntry.unique_in(self.volumes_dir, "compact") as temp:
            with temp.path.open("wb") as out, old_path.open("rb") as src:
                needle_start = 0
                for digest, locator in live:
                    out.write(str(digest).encode("ascii"))
                    out.write(_SIZE_STRUCT.pack(locator.size))
                    src.seek(locator.payload_offset)
                    _copy_stream(src, out, locator.size)
                    new_locators.append(
                        (
                            digest,
                            NeedleLocator(new_id, needle_start + NEEDLE_HEADER_LEN, locator.size),
                        )
                    )
                    needle_start += self.needle_len(locator.size)
                out.flush()

                os.fsync(out.fileno())
            publish_temp(temp.path, self.volume_path(new_id))
            temp.disarm()

        with self._index_lock:
            for digest, locator in new_locators:
                record = self._index.get(digest)
                # Tombstoned or quarantined *during* the copy: leave it pointing
                # at the old volume so the sweep below drops the row. The bytes
                # we just copied become orphans in the new volume, reclaimed by
                # a later pass — which is the safe direction to be wrong in.
                if record is not None and record.is_live:
                    record.locator = locator
            dropped = self._drop_volume_rows(old_id)

        self.checkpoint()
        old_path.unlink(missing_ok=True)
        return dropped

    def _drop_volume_rows(self, volume_id: VolumeId) -> list[Digest]:
        """Remove every index row still pointing at `volume_id`. Caller locks."""
        with self._index_lock:
            dropped = [
                digest
                for digest, record in self._index.items()
                if record.locator.volume_id == volume_id
            ]
            for digest in dropped:
                del self._index[digest]
        return dropped

    # ── recovery ────────────────────────────────────────────────────────────

    def _rebuild_from_volumes(self) -> dict[Digest, NeedleRecord]:
        """Scan every `*.dat` and rebuild the needle map from the frames.

        The recovery path, used only when the snapshot and the log are both
        gone. Tombstone and quarantine flags cannot be recovered this way — they
        exist only in the index — so a rebuilt map resurrects logically deleted
        needles. That is the accepted trade: serving a deleted object beats
        losing every live one.
        """
        index: dict[Digest, NeedleRecord] = {}
        for entry in sorted(self.volumes_dir.iterdir()):
            volume_id = _parse_volume_id(entry.name)
            if volume_id is None or not entry.is_file():
                continue
            index.update(self._scan_volume(volume_id, entry))
        return index

    @staticmethod
    def _scan_volume(volume_id: VolumeId, path: Path) -> dict[Digest, NeedleRecord]:
        """Index every complete needle in one volume, truncating a torn tail.

        Stops at the first frame that does not parse — a short header, a digest
        that is not hex, a payload running past EOF — and truncates the file to
        the end of the last good needle. Leaving the torn bytes would let a
        future append start mid-frame, turning one bad record into an
        unrecoverable file.
        """
        needles: dict[Digest, NeedleRecord] = {}
        with path.open("r+b") as handle:
            file_len = path.stat().st_size
            good_end = 0
            position = 0
            while position + NEEDLE_HEADER_LEN <= file_len:
                handle.seek(position)
                header = handle.read(NEEDLE_HEADER_LEN)
                if len(header) < NEEDLE_HEADER_LEN:
                    break
                try:
                    digest = Digest(header[: Digest.LEN].decode("ascii"))
                except (UnicodeDecodeError, InvalidRequest):
                    break
                (size,) = _SIZE_STRUCT.unpack(header[Digest.LEN :])
                payload_start = position + NEEDLE_HEADER_LEN
                if payload_start + size > file_len:
                    break
                needles[digest] = NeedleRecord(NeedleLocator(volume_id, payload_start, size))
                good_end = payload_start + size
                position = good_end

            if good_end < file_len:
                logger.warning(
                    "haystack volume had a torn tail; truncating",
                    volume=volume_id,
                    kept_bytes=good_end,
                    dropped_bytes=file_len - good_end,
                )
                handle.truncate(good_end)
        return needles

    def close(self) -> None:
        """Close the active append handle. Called on shutdown."""
        with self._active_lock:
            if self._active is not None:
                self._active.handle.close()
                self._active = None


class NeedleReader:
    """A bounded reader over one needle's payload inside a volume file.

    Exists because a volume is a single file: an ordinary handle would read
    straight past the end of this needle into the next object's bytes. It caps
    every read at the remaining payload length and closes the underlying handle
    when exhausted, so the streaming GET path can treat it like any other file
    object.
    """

    __slots__ = ("_handle", "_remaining")

    def __init__(self, handle: BinaryIO, length: int) -> None:
        self._handle = handle
        self._remaining = length

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size < 0 else min(size, self._remaining)
        chunk = self._handle.read(want)
        self._remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> NeedleReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[bytes]:
        while chunk := self.read(64 * 1024):
            yield chunk


def _copy_stream(source: BinaryIO, dest: BinaryIO, length: int) -> None:
    """Copy exactly `length` bytes in bounded chunks.

    Not `shutil.copyfileobj`: that copies to EOF, and here the source is either
    a temp file we want all of or a volume we want one needle out of. The
    explicit remaining-count is what makes both cases the same code.
    """
    remaining = length
    while remaining > 0:
        chunk = source.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise InvalidRequest(f"unexpected end of input: {remaining} bytes short of {length}")
        dest.write(chunk)
        remaining -= len(chunk)
