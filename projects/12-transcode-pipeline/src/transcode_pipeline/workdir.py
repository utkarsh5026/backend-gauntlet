"""The artifact store's layout: where sources, chunks and renditions live on disk.

Everything a job reads or writes lives under `WORK_DIR`:

    WORK_DIR/
      bbb.mp4                        ← a source a client names in POST /jobs
      jobs/<job_id>/
        720p/chunks/0.mp4 1.mp4 …    ← V3's per-chunk transcodes
        720p/out.mp4                 ← V4's stitched rendition

Postgres holds the *graph*; this directory holds the *bytes*. The layout methods
are wired. `resolve_source` — the path-traversal guard — is a Security checklist
item, and yours.

Two things about paths that bite in Python exactly as they do everywhere else:

* `root / "/etc/passwd"` is `/etc/passwd`. Joining an absolute path onto a base
  **discards the base** — `pathlib` follows `os.path.join` here.
* A rendition `name` is a path segment too. `chunk_dir(job, "../../../tmp")` walks
  out of `WORK_DIR` on *write*, which is worse than a read. The ladder a client
  sends is untrusted in exactly the way `source` is (see `models.Rendition`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

__all__ = ["WorkDir"]


@dataclass(frozen=True, slots=True)
class WorkDir:
    """The artifact root and the paths derived from it."""

    root: Path

    def job_dir(self, job_id: UUID) -> Path:
        """Per-job artifact root: `WORK_DIR/jobs/<job_id>`."""
        return self.root / "jobs" / str(job_id)

    def chunk_dir(self, job_id: UUID, rendition: str) -> Path:
        """One rendition's transcoded chunk files, before stitching:
        `…/<job_id>/<rendition>/chunks`."""
        return self.job_dir(job_id) / rendition / "chunks"

    def rendition_output(self, job_id: UUID, rendition: str) -> Path:
        """The final stitched output for a rendition: `…/<job_id>/<rendition>/out.mp4`."""
        return self.job_dir(job_id) / rendition / "out.mp4"

    def resolve_source(self, source: str) -> Path:
        """Resolve a client-supplied source path so it can never escape `WORK_DIR`.

        TODO(security): the traversal guard. Resolve the candidate to its *real*
        path and reject anything not under `root`: `..`, absolute paths, and a
        symlink inside `WORK_DIR` that points outside it. A string check on the raw
        input cannot see a symlink; only resolving through the filesystem can. And
        "is under" is a relation between paths, not a string prefix — the string
        `/srv/work-evil` starts with `/srv/work`. A bad path is a `BadRequestError`
        or `NotFoundError` whose message never echoes what the filesystem said.

        Resolving touches the filesystem, so this is a (short) blocking call. The
        worker runs it in a thread; if you call it from `POST /jobs`, do the same.
        """
        raise NotImplementedError(
            "security: resolve `source` under WORK_DIR without allowing traversal"
        )
