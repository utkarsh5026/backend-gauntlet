"""Crash-safe publish, in one place: the temp → fsync → rename → fsync-dir dance.

Every durable write in this store obeys the same contract: a final path may only
appear once its bytes are fully on disk, and the *rename that makes it appear*
must itself survive a crash. That takes four steps in a fixed order, and getting
any one of them wrong is a silent data-loss bug rather than a test failure — so
it lives here once instead of being re-derived at each call site.

## Why each step is there

1. `fsync(temp)` — the bytes are durable **before** anything can see the name.
   Skip it and a crash can leave the rename visible while the content it points
   at is still in the page cache and gone.
2. `mkdir -p dest.parent` — the destination tree exists, so the rename cannot
   fail halfway through publishing.
3. `rename(temp, dest)` — atomic *within one filesystem*. This is the whole
   trick: `dest` flips from absent to complete with no observable in-between,
   so no reader ever sees a truncated blob under its final name.
4. `fsync(dest.parent)` — the **directory entry** is durable. Without it the
   rename can be rewound by a crash even though the file's own bytes were
   synced: the name lives in the directory, not in the file.

## Same-filesystem invariant

`rename` is only atomic within one filesystem, and across two it fails outright
with `EXDEV`. Callers must stage a temp on the same filesystem as `dest` — the
per-bucket `tmp/` dirs already are, and `atomic_write_sibling` guarantees it by
construction by staging next to the destination.

## Blocking on purpose

Everything here is synchronous. `fsync` has no asynchronous form on Linux, so
"async file I/O" in any runtime is a thread pool with a nicer face — tokio's
`fs` module included. Making that explicit means the call sites have to say
`await asyncio.to_thread(...)`, which is exactly the honesty this project wants:
you can see where the loop hands work to a thread.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from types import TracebackType
from typing import Any

__all__ = [
    "TempEntry",
    "atomic_write",
    "atomic_write_json",
    "atomic_write_sibling",
    "publish_temp",
    "unique_temp_path",
]


def unique_temp_path(directory: Path, prefix: str) -> Path:
    """A unique staging path under `directory`, labelled with `prefix`.

    Uniqueness comes from `secrets.token_hex`, not a timestamp: two writes in
    the same nanosecond are rare but two writes in the same clock *tick* are
    not, and a collision here means one upload silently overwrites another's
    staged bytes.
    """
    return directory / f"{prefix}-{secrets.token_hex(8)}"


class TempEntry:
    """Deletes a staged temp file on exit unless ownership is disarmed.

    The pattern is stage → hash/write → publish. Any early exit — a size cap
    trip, an I/O error, a validation failure, a cancelled request — unlinks the
    half-written temp automatically, so no error path has to remember to clean
    up. `disarm()` hands ownership away once the bytes are durably published and
    the file is no longer garbage.

    Usable as a context manager (preferred) or manually, since a few call sites
    hold one across an await boundary where `with` would not fit.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path: Path | None = path

    @classmethod
    def unique_in(cls, directory: Path, prefix: str) -> TempEntry:
        """A guarded unique path under `directory` for staging an in-flight write."""
        return cls(unique_temp_path(directory, prefix))

    @property
    def path(self) -> Path:
        """Where in-flight bytes should be staged."""
        if self._path is None:
            raise RuntimeError("temp entry was already disarmed")
        return self._path

    def disarm(self) -> None:
        """Give up ownership so the file survives — call after publishing."""
        self._path = None

    def cleanup(self) -> None:
        """Unlink the staged file if this guard still owns one. Idempotent."""
        path, self._path = self._path, None
        if path is not None:
            path.unlink(missing_ok=True)

    def __enter__(self) -> TempEntry:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.cleanup()


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it is durable.

    Needs `O_RDONLY` on the directory itself — you cannot open a directory for
    writing — which is why this is a raw `os.open` rather than `Path.open`.
    """
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_temp(temp: Path, dest: Path) -> None:
    """Durably publish an already-written temp file at `dest`.

    The four steps from the module docstring, in order — reorder nothing.

    Cleaning up `temp` on the error path is the **caller's** job (wrap it in a
    `TempEntry`): only the caller knows which staging area it came from.
    """
    fd = os.open(temp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp, dest)
    _fsync_dir(dest.parent)


def atomic_write(temp: Path, dest: Path, data: bytes) -> None:
    """Write `data` to the caller's staging path, then publish it at `dest`.

    The caller chooses `temp` — the index stages under its per-bucket `tmp/` so
    GC's in-flight scan can see the digests of a write that has not landed yet —
    and owns cleanup on failure.
    """
    temp.parent.mkdir(parents=True, exist_ok=True)
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    publish_temp(temp, dest)


def atomic_write_sibling(dest: Path, data: bytes) -> None:
    """Atomically write `data` to `dest`, staging next to it.

    Staging in `dest`'s own directory is the cheapest way to guarantee the
    same-filesystem invariant: two paths in one directory are always on one
    filesystem, so `rename` cannot fail with `EXDEV`.
    """
    with TempEntry.unique_in(dest.parent, f"{dest.name}.tmp") as temp:
        atomic_write(temp.path, dest, data)
        temp.disarm()


def atomic_write_json(temp: Path, dest: Path, value: Any) -> None:
    """Serialise `value` as JSON and `atomic_write` it through `temp`."""
    atomic_write(temp, dest, json.dumps(value).encode("utf-8"))
