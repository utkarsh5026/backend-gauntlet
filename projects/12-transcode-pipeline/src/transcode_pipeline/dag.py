"""V2 — The job DAG: model the pipeline as a dependency graph and schedule it.

A transcode job isn't a queue of independent jobs — it's a graph with a shape:

                       ┌── transcode(chunk 0, 720p) ─┐
                       ├── transcode(chunk 1, 720p) ─┤
      split ──fan-out──┼── transcode(chunk 2, 720p) ─┼─fan-in─→ stitch(720p)
                       ├── transcode(chunk 0, 480p) ─┤
                       └──          …                ┴─fan-in─→ stitch(480p)

Two functions here are the vertical:

* **`expand`** — build the transcode + stitch tasks (and their edges) once the
  `Split` has discovered the chunk count. This is where the DAG's *shape* is
  defined: every `Transcode` depends on the `Split`; every `Stitch` depends on
  *all* of its rendition's `Transcode`s (the fan-in the boss exploits).
* **`newly_ready`** — the scheduler's core: given the current task states, which
  `PENDING` tasks now have *all* dependencies `DONE` and so become runnable? The
  pure, in-memory twin of `JobStore.promote_ready`, and the one you property-test.

`schedule_loop` is the wired background pass that drives the store forward, and
`deps_all_done` is the wired one-line spec of "runnable".

## Pure on purpose

Both vertical functions take plain values and return plain values — no pool, no
`await`. That is what lets a test assert the fan-in shape of a 200-chunk,
3-rendition job in a millisecond, and what makes `newly_ready` a *specification*
the SQL in `promote_ready` can be checked against: feed both the same task states
and compare the answers.

## Proving it

`test_expand_wires_fan_in` (the edge shape for a 2-rendition, N-chunk job) and
`test_stitch_waits_for_all_chunks` (a stitch is withheld until its last chunk flips
`DONE`) are pure; `test_dag_resumes_after_restart` needs Postgres — the
`pg_pool` fixture in `tests/conftest.py` hands each test its own migrated database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Sequence
from uuid import UUID

import structlog

from .chunk import ChunkPlan
from .models import Rendition, Task, TaskStatus
from .state import wait_for_shutdown
from .store import JobStore

__all__ = ["deps_all_done", "expand", "newly_ready", "schedule_loop"]

logger = structlog.get_logger(__name__)


def expand(
    job_id: UUID, split_task: UUID, chunks: Sequence[ChunkPlan], ladder: Sequence[Rendition]
) -> list[Task]:
    """Build every downstream task for a job once its chunks are known.

    `split_task` is the already-running `Split` the transcodes depend on; `chunks`
    is V1's plan; `ladder` is the job's output ladder. The returned tasks are what
    `JobStore.add_tasks` persists.

    TODO(V2): construct the graph.
      - For each rendition × each chunk, a `Transcode(chunk=…, rendition=…)` task
        that depends on `split_task`, status `PENDING`.
      - For each rendition, one `Stitch(rendition=…)` task that depends on
        **every** `Transcode` of that rendition and nothing else — the fan-in join.
      - Fresh ids minted here with `uuid.uuid4()`, not by the database's
        `gen_random_uuid()` default: a stitch's edges have to name its transcodes'
        ids *before* any row exists. `attempts=0`, `lease_until=None`.
    """
    raise NotImplementedError(
        "V2: build transcode tasks (dep: split) + per-rendition stitch tasks "
        "(dep: all their transcodes)"
    )


def newly_ready(tasks: Collection[Task]) -> list[UUID]:
    """The scheduler's readiness rule: which `PENDING` tasks are now runnable?

    A task becomes ready exactly when **all** of its dependencies are `DONE`. A task
    with no dependencies is ready immediately.

    TODO(V2): return the ids of every `PENDING` task in `tasks` whose `deps` all
    resolve to a `DONE` task in the same collection. Keep it pure — it's the
    testable heart of the scheduler and the mirror of `JobStore.promote_ready`.

    Mind the cost on a real job: 3,600 transcodes each answering "is my dependency
    done?" by scanning the collection is millions of comparisons per scheduler tick,
    where a `set` of done ids built once makes each answer a hash lookup. And decide
    what a dependency that names a task *not in the collection* means — that is the
    unsatisfiable edge the SPEC calls deadlock, and treating it as done is the
    opposite of safe.
    """
    raise NotImplementedError("V2: return PENDING tasks whose every dependency is DONE")


def deps_all_done(task: Task, is_done: Callable[[UUID], bool]) -> bool:
    """A task is runnable iff it is pending and every dependency is done.

    Wired, and kept tiny and total so `newly_ready` has an obvious spec to be tested
    against.
    """
    return task.status is TaskStatus.PENDING and all(is_done(dep) for dep in task.deps)


async def _pass(log: structlog.typing.FilteringBoundLogger, name: str, op: Awaitable[int]) -> None:
    """Run one store pass, logging what it moved.

    A missing vertical propagates and ends the scheduler — `main` logs the todo
    once. Any other failure (Postgres blipped) is logged and the next tick tries
    again: one bad tick must not stop scheduling for good, or every job stalls with
    nothing in the log after the first error.
    """
    try:
        moved = await op
    except NotImplementedError:
        raise
    except Exception as exc:
        log.error(f"{name} failed", error=str(exc), kind=type(exc).__name__)
        return
    if moved:
        log.debug(f"{name} moved tasks", count=moved)


async def schedule_loop(store: JobStore, interval_secs: float, shutdown: asyncio.Event) -> None:
    """Background scheduler + reaper: on a fixed tick, promote newly-ready tasks and
    reclaim tasks whose worker's lease expired.

    Wired for you — it only calls store methods you implement (`promote_ready` is
    V2, `reclaim_expired` is V3). Until they exist the first tick raises, the task
    ends, and the done-callback in `main` logs the todo that stopped it.
    """
    log = logger.bind(loop="scheduler")
    log.info("scheduler started", interval_secs=interval_secs)
    while not await wait_for_shutdown(shutdown, interval_secs):
        # V2: PENDING → READY when deps are DONE. This is what makes progress flow
        # along the DAG as transcodes finish.
        await _pass(log, "promote_ready", store.promote_ready())

        # V3: reclaim tasks a dead worker was holding — the reaper half of the
        # lease. Without it, one crashed worker strands its chunk and the fan-in
        # stitch waits forever (the boss's straggler).
        await _pass(log, "reclaim_expired", store.reclaim_expired())
    log.info("scheduler stopped")
