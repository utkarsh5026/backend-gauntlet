-- The jobs table. One row per job; the `state` column is its lifecycle
-- (ready -> running -> done | dead). This single table backs the whole queue:
-- the V1 claim, the V2 lease, the V3 retry/DLQ, and V4 scheduling all read and
-- write columns here. Postgres is doing double duty as durable store AND broker.
CREATE TABLE IF NOT EXISTS jobs (
    id            BIGSERIAL   PRIMARY KEY,
    queue         TEXT        NOT NULL DEFAULT 'default',
    kind          TEXT        NOT NULL,                  -- which handler runs this
    payload       JSONB       NOT NULL DEFAULT '{}',     -- opaque job arguments
    state         TEXT        NOT NULL DEFAULT 'ready',  -- ready|running|done|dead

    -- Retries (V3): attempts so far, the ceiling, and the last failure reason.
    attempts      INT         NOT NULL DEFAULT 0,
    max_attempts  INT         NOT NULL DEFAULT 5,
    last_error    TEXT,

    -- Scheduling / delayed delivery (V4). A job is only claimable once
    -- run_at <= now(); enqueue-with-delay and retry-backoff both push this out.
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Visibility timeout / lease (V2). A worker stamps these when it claims a job;
    -- the reaper requeues rows whose locked_until has passed (crashed worker).
    locked_at     TIMESTAMPTZ,
    locked_until  TIMESTAMPTZ,
    locked_by     TEXT,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

