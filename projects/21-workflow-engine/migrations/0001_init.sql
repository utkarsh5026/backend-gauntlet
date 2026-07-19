-- The workflow-engine schema. Four tables, one job each:
--   workflow_executions — one row per execution attempt (run): status + terminal result.
--   history_events      — the append-only event log; folding it rebuilds any execution.
--   task_queue          — workflow/activity tasks the dispatcher hands to workers (V4).
--   timers              — durable timers, so a long sleep outlives every process (V3).
--
-- The history is the STATE. There is no mutable "current state" column that a balance
-- or step counter lives in; state is DERIVED by replaying history_events (V1/V2). That
-- immutability is exactly what lets a fresh worker resume a half-run execution after a
-- crash with no lost or duplicated work.

-- Postgres provides gen_random_uuid() via pgcrypto on modern builds; enable it so a
-- run id needs no coordinated sequence.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- One row per execution attempt. `workflow_id` is the caller's logical id; `run_id`
-- names this particular attempt. Terminal state (result/failure) is a convenience
-- projection you MAY cache here on completion — but the truth is always the history.
CREATE TABLE IF NOT EXISTS workflow_executions (
    run_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id   TEXT        NOT NULL,
    workflow_type TEXT        NOT NULL,
    task_queue    TEXT        NOT NULL,
    -- running | completed | failed. A projection of the last history event, not a
    -- second source of truth (V1's derived-state rule).
    status        TEXT        NOT NULL DEFAULT 'running',
    result        BYTEA,                                   -- set on WORKFLOW_COMPLETED
    failure       TEXT,                                    -- set on WORKFLOW_FAILED
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TODO(V4): a re-use policy usually wants "at most one RUNNING execution per
-- workflow_id". Add the partial unique index that enforces it, and decide what a
-- duplicate StartWorkflow does (reject? return the running run?). Left out on purpose.
--   CREATE UNIQUE INDEX ... ON workflow_executions (workflow_id) WHERE status = 'running';

-- The heart of the engine: the append-only event log. `event_id` is monotonic PER run
-- and defines replay order — (run_id, event_id) is the primary key, so a duplicate
-- append is a key violation, not a silent double-write. Events are NEVER updated or
-- deleted; a correction is a new event, never an edit.
CREATE TABLE IF NOT EXISTS history_events (
    run_id       UUID        NOT NULL REFERENCES workflow_executions(run_id),
    event_id     BIGINT      NOT NULL,                     -- monotonic per run (1,2,3,…)
    event_type   TEXT        NOT NULL,                     -- see model::EventType
    attributes   JSONB       NOT NULL DEFAULT '{}'::jsonb, -- event-type-specific payload
    timestamp_ms BIGINT      NOT NULL,                     -- epoch millis the event was recorded
    PRIMARY KEY (run_id, event_id)
);

-- The task queues the dispatcher claims from (V4). A task is a POINTER into history
-- (which run, which scheduled event), not a copy of the work — the worker fetches the
-- history when it polls. `state` + `visible_at` implement at-least-once delivery:
-- a claimed task gets a visibility timeout; if the worker dies without completing it,
-- it becomes claimable again (that requeue is what beats The Reaper).
CREATE TABLE IF NOT EXISTS task_queue (
    id                 BIGSERIAL   PRIMARY KEY,
    task_queue         TEXT        NOT NULL,
    kind               TEXT        NOT NULL,               -- 'workflow' | 'activity'
    run_id             UUID        NOT NULL REFERENCES workflow_executions(run_id),
    scheduled_event_id BIGINT      NOT NULL,               -- the ACTIVITY_SCHEDULED / task-scheduled event
    state              TEXT        NOT NULL DEFAULT 'pending', -- pending | started
    -- When this task next becomes claimable. now() = ready; a claim pushes it out by
    -- the visibility timeout; a completion deletes the row.
    visible_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Which worker holds the claim (for sticky routing / debugging).
    locked_by          TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TODO(V4): the poll/claim query is roughly
--     SELECT ... FROM task_queue
--     WHERE task_queue = $1 AND kind = $2 AND visible_at <= now()
--     ORDER BY visible_at
--     FOR UPDATE SKIP LOCKED
--     LIMIT 1
-- (SKIP LOCKED so two polling workers never grab the same task). Add the partial
-- index over (task_queue, kind, visible_at) that keeps it cheap under load, and prove
-- the before/after. Left out on purpose — it's a V4 lesson.

-- Durable timers (V3). A StartTimer command inserts a row here inside the SAME
-- transaction that appends TIMER_STARTED, so a timer can never be lost with the
-- process that created it. The timer service scans for due rows and, exactly once,
-- appends TIMER_FIRED + schedules a workflow task. (run_id, timer_id) is the key so a
-- re-scan can't fire the same timer twice.
CREATE TABLE IF NOT EXISTS timers (
    run_id            UUID        NOT NULL REFERENCES workflow_executions(run_id),
    timer_id          TEXT        NOT NULL,                -- workflow-assigned id
    started_event_id  BIGINT      NOT NULL,                -- the TIMER_STARTED event
    fire_at           TIMESTAMPTZ NOT NULL,                -- when it should fire
    state             TEXT        NOT NULL DEFAULT 'pending', -- pending | fired
    PRIMARY KEY (run_id, timer_id)
);

-- TODO(V3): the scan query is
--     SELECT ... FROM timers WHERE state = 'pending' AND fire_at <= now()
--     ORDER BY fire_at FOR UPDATE SKIP LOCKED LIMIT $1
-- Add the partial index over (fire_at) WHERE state = 'pending' that keeps the scan
-- O(due) instead of O(all timers), and measure it. Left out on purpose.
