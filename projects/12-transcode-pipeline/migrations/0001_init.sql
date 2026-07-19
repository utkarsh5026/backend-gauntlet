-- Distributed transcoding pipeline — the durable job DAG.
--
-- Three tables: `jobs` (one per submitted asset), `tasks` (the DAG nodes), and
-- `task_deps` (the edges). Artifacts (chunk files, finished renditions) live on
-- the filesystem under WORK_DIR, not here — Postgres holds only the *graph* and
-- its state, which is what must survive a crash and coordinate many workers.
--
-- This is a starting schema for the V2/V3 store methods in `src/job.rs`; refine
-- it as you implement (indexes for the claim, a partial index on Ready tasks,
-- etc.). It intentionally leaves the interesting queries to you.

-- Job / task lifecycle. Mirrors the `Status` enum in src/job.rs.
CREATE TYPE task_status AS ENUM ('pending', 'ready', 'running', 'done', 'failed');
CREATE TYPE job_status AS ENUM ('running', 'done', 'failed');

CREATE TABLE jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Source path, relative to WORK_DIR (resolved + traversal-checked in code).
    source      TEXT        NOT NULL,
    -- The output ABR ladder for this job (array of {name,height,bitrates}).
    ladder      JSONB       NOT NULL,
    status      job_status  NOT NULL DEFAULT 'running',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tasks (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id       UUID        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    -- Discriminated task kind: {"op":"split"} | {"op":"transcode","chunk":N,"rendition":"720p"}
    -- | {"op":"stitch","rendition":"720p"}. Matches the TaskKind enum in code.
    kind         JSONB       NOT NULL,
    status       task_status NOT NULL DEFAULT 'pending',
    attempts     INT         NOT NULL DEFAULT 0,
    -- Set while Running; the reaper reclaims the task once this passes.
    lease_until  TIMESTAMPTZ,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- DAG edges: `task_id` depends on `depends_on` (must be Done before `task_id` is
-- Ready). A composite PK keeps edges unique.
CREATE TABLE task_deps (
    task_id     UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on  UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

-- The claim path (V3) scans Ready tasks; the reaper scans expired Running leases.
CREATE INDEX tasks_status_idx ON tasks (status);
CREATE INDEX tasks_lease_idx ON tasks (lease_until) WHERE status = 'running';
CREATE INDEX tasks_job_idx ON tasks (job_id);
CREATE INDEX task_deps_depends_on_idx ON task_deps (depends_on);
