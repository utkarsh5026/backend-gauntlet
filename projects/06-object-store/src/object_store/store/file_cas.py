"""One-file-per-digest CAS layout — the default physical store.

Each distinct blob lives at `objects/<ab>/<cd>/<sha256>` and is published with
the shared temp → fsync → rename → fsync-dir dance from `durable`. This is the
layout the whole project is built around; `haystack` is the packed alternative
for the small-object case.

## The fan-out, and why two levels

`objects/<64-hex>` in one flat directory melts at a few million entries: ext4's
htree lookups degrade, `readdir` of the directory becomes a full scan, and every
tool that lists it (including our own GC) stalls. Sharding on the first two
bytes of the digest gives 256 × 256 = 65,536 leaf directories, so ten million
blobs sit ~150 per directory — comfortably inside what every filesystem handles
without special cases.

Two levels rather than one because one level (256 dirs) only buys a 256× cut,
which the same ten million blobs would blow straight through at ~39,000 entries
each. Three levels would be 16.7M directories, most of them empty, and the inode
cost of the tree starts to rival the blobs. Two is the number the digest hands
you for free: the hash is uniform, so the shards are uniform, with no rebalancing
and no hot directory.

Everything here is **synchronous**; `Store` is the layer that moves it off the
event loop with `asyncio.to_thread`. See `durable` on why that is honest rather
than lazy.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ..durable import publish_temp
from ..errors import NoSuchKey
from ..objects import Digest

__all__ = ["FileCas"]

SHARD_WIDTH = 2
"""Hex characters per shard level — one byte of the digest."""

SHARD_LEVELS = 2
"""Directory levels before the blob file itself. See the module docstring."""


class FileCas:
    """A content-addressed tree: one committed file per digest under `objects/`."""

    __slots__ = ("objects_root",)

    def __init__(self, root: Path) -> None:
        self.objects_root = root / "objects"
        self.objects_root.mkdir(parents=True, exist_ok=True)

    def blob_path(self, digest: Digest) -> Path:
        """Map a digest to its sharded on-disk path (`objects/ab/cd/<64-hex>`)."""
        return self.objects_root / digest[0:2] / digest[2:4] / digest

    def digest_from_path(self, path: Path) -> Digest | None:
        """Recover a digest from a sharded blob path — the inverse of `blob_path`.

        Deliberately strict: only paths under `objects_root` with exactly the
        `ab/cd/<64-hex>` shape are accepted. GC and the scrubber both walk this
        tree and act on what comes back, so a lenient parse here would let a
        stray file (an editor backup, a half-copied blob) be mistaken for a
        content address and deleted or quarantined under it.
        """
        try:
            relative = path.relative_to(self.objects_root)
        except ValueError:
            return None

        parts = relative.parts
        if len(parts) != SHARD_LEVELS + 1:
            return None
        shard_a, shard_b, name = parts
        if len(shard_a) != SHARD_WIDTH or len(shard_b) != SHARD_WIDTH:
            return None
        if not name.startswith(shard_a + shard_b):
            return None
        try:
            return Digest(name)
        except Exception:
            return None

    def iter_blob_files(self) -> Iterator[Path]:
        """Every regular file under the blob tree, depth-first.

        `Path.rglob` rather than a hand-rolled stack: the tree is exactly two
        levels deep and this reads as what it is.
        """
        for path in self.objects_root.rglob("*"):
            if path.is_file():
                yield path

    def list_digests(self) -> list[Digest]:
        """Every committed digest found on disk — the locator map's boot input."""
        return [
            digest
            for path in self.iter_blob_files()
            if (digest := self.digest_from_path(path)) is not None
        ]

    def scan_occupancy(self) -> tuple[int, int]:
        """`(blob_count, total_bytes)` over the tree, for the boot gauges."""
        count = 0
        total = 0
        for path in self.iter_blob_files():
            count += 1
            total += path.stat().st_size
        return count, total

    def contains(self, digest: Digest) -> bool:
        """Whether a committed blob file exists for `digest` in this layout."""
        return self.blob_path(digest).is_file()

    def commit_temp(self, temp: Path, digest: Digest) -> None:
        """Publish a fully written temp file at its content-addressed path.

        The dedup short-circuit, the metrics and waking the scrubber are all
        `Store`'s job — this layer only knows how to put bytes somewhere safely.
        """
        publish_temp(temp, self.blob_path(digest))

    def open_blob(self, digest: Digest):  # noqa: ANN201 - BufferedReader
        """Open a committed blob for reading.

        No quarantine check: that state lives on `Store`, which owns the read
        gate. A layout does not get to have opinions about integrity.
        """
        path = self.blob_path(digest)
        if not path.is_file():
            raise NoSuchKey()
        return path.open("rb")

    def remove(self, digest: Digest) -> int | None:
        """Remove a committed blob if present; return its size, or `None`.

        Idempotent — GC and lifecycle both call it, and a blob already reclaimed
        by one of them is not an error for the other.
        """
        path = self.blob_path(digest)
        if not path.is_file():
            return None
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        return size
