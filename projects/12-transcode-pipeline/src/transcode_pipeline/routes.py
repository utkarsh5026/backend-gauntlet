"""HTTP surface: the control-plane API. Workers consume the DAG out of band
(`worker.py`); this is how a job gets *submitted* and *inspected*.

Handlers are thin and wired; what they call — `store.submit` / `store.get_job` — is
where V2 lives. Run it as-is and `POST /jobs` answers `501` naming the V2 todo,
which is the worklist.

What the framework already does, both on the horizontal checklist: a body without a
`source` is a `422` before the handler runs, and so is a job id that isn't a UUID —
pydantic parses the path parameter. The return annotations make the job view a
published schema at `/docs`, which is half of "stable, documented JSON".
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import PlainTextResponse

from .errors import NotFoundError
from .models import JobAccepted, JobView, NewJob
from .state import AppState, get_state

__all__ = ["router"]

router = APIRouter()

State = Annotated[AppState, Depends(get_state)]


@router.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    """Liveness. Touches nothing — not Postgres, not ffmpeg — and needs no auth: the
    orchestrator's probe has to reach it."""
    return "ok"


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def submit(body: NewJob, state: State) -> JobAccepted:
    """`POST /jobs` — submit a transcode job; V2 seeds its DAG.

    `202 Accepted`, not `201 Created`: the work has been *recorded*, not done, and
    the id is what the caller polls.

    TODO(security): authenticate before doing anything — an open submit lets anyone
    make your workers run ffmpeg on arbitrary inputs. Then validate the body: the
    `source` must resolve under `WORK_DIR` (`WorkDir.resolve_source`), and the ladder
    must be sane — a bounded number of rungs, bounded heights and bitrates, and names
    that are safe to use as a directory.
    """
    # Fall back to the server default ladder when the request doesn't pin one.
    ladder = body.ladder or state.settings.default_ladder
    job_id = await state.store.submit(body.source, ladder)
    return JobAccepted(id=job_id)


@router.get("/jobs/{job_id}")
async def get_job(job_id: UUID, state: State) -> JobView:
    """`GET /jobs/{id}` — job status + per-status task counts, so a caller can watch
    the DAG drain. An unknown id is a clean `404`."""
    view = await state.store.get_job(job_id)
    if view is None:
        raise NotFoundError("job not found")
    return view
