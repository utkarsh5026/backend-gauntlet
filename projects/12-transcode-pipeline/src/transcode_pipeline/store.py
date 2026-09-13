"""The durable DAG store — V2's rows and V3's lease.

Every method is a Postgres round-trip against the `jobs` / `tasks` / `task_deps`
schema in `migrations/0001_init.sql`. The graph, its edges, and every task's status
+ lease live in rows, so a crashed worker's task becomes claimable again and the
pipeline survives a restart: kill the coordinator mid-job, start it again, and the
scheduler's next pass reads the same states and carries on. Nothing is remembered
only in memory.

## Raw SQL, on purpose

asyncpg with `$1`-style parameterized statements, not an ORM. V2's readiness rule
*is* a query (`promote_ready`), V3's claim *is* a query (`FOR UPDATE SKIP LOCKED`),
and the SPEC grades those statements — an ORM would hide exactly the part you are
here to write. Parameters travel to the server separately from the SQL text, so
there is no string-built SQL and no injection surface. Keep it that way.

## asyncpg facts that shape these methods

* The pool's connections decode `JSONB` into Python objects (see `db.py`), so a
  `kind` column arrives as a `dict`: `TASK_KIND.validate_python(row["kind"])` makes
  it a `Split | Transcode | Stitch`, and `Rendition.model_validate` does the same
  for a ladder rung. Going the other way, pass `model_dump(mode="json")`, not the
  model.
* A `datetime.timedelta` parameter binds to an `interval`, so a lease can be
  computed by the database's `now()` — one clock for every worker process, instead
  of each worker's own.
* `execute()` returns the command tag — the *string* `"UPDATE 12"` — not a row
  count. `RETURNING` with `fetch()` gives you rows when you need to know which.
* A multi-statement change is atomic only inside `async with conn.transaction():`
  on one acquired connection. Two `pool.execute` calls are two connections and two
  transactions.

Scaffold state: the store is constructed over the pool; every method is the V2/V3
worklist.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import asyncpg

from .models import JobContext, JobView, Rendition, Task

__all__ = ["JobStore"]


class JobStore:
    """The durable DAG: jobs, tasks, dependency edges, and leases."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool[asyncpg.Record]:
        """The underlying pool, for the parts of V2/V3 that want a transaction."""
        return self._pool

    # ---- V2: the graph ----------------------------------------------------------

    async def submit(self, source: str, ladder: Sequence[Rendition]) -> UUID:
        """Create a job and seed its DAG with a single `READY` `Split` task.

        TODO(V2): in **one transaction**, insert the `jobs` row (source + ladder)
        and the seed `Split` task (status `READY`, no deps); return the job id. The
        rest of the graph is discovered later, when a worker runs the `Split` and
        calls `add_tasks`. A job row that commits without its seed task is a job
        nothing will ever run — and `202 Accepted` would have been a lie.
        """
        raise NotImplementedError("V2: insert job + seed Split task (READY), return the job id")

    async def get_job(self, job_id: UUID) -> JobView | None:
        """The read view of a job with its per-status task counts; `None` if unknown.

        TODO(V2): the job row plus a `GROUP BY status` over its tasks. Whether the
        job's `status` is read from `jobs.status` or *derived* from the task counts
        is a design decision (`docs/01-work-as-a-dag-not-a-queue.md` §6) — either
        way it must be `done` only when every task is, and `failed` if any task is.
        """
        raise NotImplementedError("V2: load the job row + GROUP BY status task counts")

    async def job_context(self, job_id: UUID) -> JobContext:
        """The source + ladder for a job — what a worker needs to run its `Split`.

        TODO(V2): a single-row lookup on `jobs`. A job that doesn't exist is a
        `NotFoundError`: the task holding its id has nothing left to run.
        """
        raise NotImplementedError("V2: return the (source, ladder) context for the job")

    async def add_tasks(self, tasks: Sequence[Task]) -> None:
        """Persist a batch of newly discovered tasks and their dependency edges —
        the `Split`'s expansion.

        New tasks land `PENDING`; the scheduler promotes them once their
        dependencies are `DONE`.

        TODO(V2): insert the `tasks` rows and the `task_deps` edges. Before choosing
        how atomic that is, work out what a crash halfway through this call leaves
        behind, and whether the scheduler could tell it apart from a healthy job.
        Hundreds of rows sent one `execute` at a time is hundreds of round-trips;
        `executemany`, or one `INSERT … SELECT … FROM unnest($1::uuid[], …)` over
        parallel arrays, is a few.
        """
        raise NotImplementedError("V2: insert tasks + their dependency edges")

    async def promote_ready(self) -> int:
        """Scheduler pass: promote every `PENDING` task whose dependencies are all
        `DONE` to `READY`. Returns how many it promoted.

        TODO(V2): the DAG readiness query — a `PENDING` task with no `task_deps` edge
        pointing at a not-yet-`DONE` task becomes `READY`. Its pure twin is
        `dag.newly_ready`: the same task states must give both the same answer.
        """
        raise NotImplementedError("V2: promote PENDING tasks whose dependencies are all DONE")

    # ---- V3: the lease -----------------------------------------------------------

    async def claim_ready(self, worker_id: str, lease_secs: float) -> Task | None:
        """Atomically claim one `READY` task for `worker_id`, flipping it to `RUNNING`
        under a fresh lease. `None` when nothing is ready.

        TODO(V3): the classic `FOR UPDATE SKIP LOCKED` claim — project 04's, reused
        rather than re-taught. Pick one `READY` task, set it `RUNNING`, bump
        `attempts`, stamp `lease_until`, and return it. A claimed task doesn't need
        its `deps` loaded. An empty queue is an ordinary answer, not an exception.
        """
        raise NotImplementedError(
            "V3: claim one READY task (FOR UPDATE SKIP LOCKED), lease it, return it"
        )

    async def complete(self, task_id: UUID) -> None:
        """Mark a task `DONE` after its artifact is committed. This is what unblocks
        downstream tasks — the scheduler notices on its next pass.

        TODO(V3): set it `DONE` and clear the lease. Under at-least-once the task may
        already be `DONE` — a slow worker finishing after its lease was reclaimed and
        the chunk re-run — and that second `complete` must be harmless.
        """
        raise NotImplementedError("V3: mark the task DONE and clear its lease")

    async def fail(self, task_id: UUID, error: str, max_attempts: int) -> None:
        """Settle a failed attempt: back to `READY` for a retry while attempts remain,
        otherwise `FAILED` — which fails the job.

        TODO(V3): compare `attempts` to `max_attempts`; requeue or dead-letter, and
        keep `error` in `last_error`. The SPEC asks for retries *with backoff*, and
        the schema has no "not before" column yet — where the delay lives is yours to
        decide.
        """
        raise NotImplementedError("V3: retry-or-dead-letter based on attempts vs max_attempts")

    async def reclaim_expired(self) -> int:
        """Reaper pass: return `RUNNING` tasks whose lease has expired — their worker
        died, or is slow enough to be treated as dead — to `READY`. Returns how many
        it reclaimed.

        TODO(V3): `RUNNING` tasks whose `lease_until` is in the past go back to
        `READY`.
        """
        raise NotImplementedError("V3: reclaim RUNNING tasks whose lease has expired to READY")
