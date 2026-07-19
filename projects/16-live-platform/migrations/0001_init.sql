-- The control-plane store. One row per stream; the `state` column is its lifecycle
-- (offline -> ingesting -> transcoding -> live -> ended). This is the source of truth
-- the control plane reconciles from on restart (V1), so a crash doesn't lose live streams.

-- Registered streams the platform will admit (a broadcaster authenticates ingest
-- with `stream_key`). Pre-seeded out of band; ingest just references them.
CREATE TABLE IF NOT EXISTS streams (
    stream_key   TEXT        PRIMARY KEY,               -- secret ingest key + URL slug
    owner        TEXT        NOT NULL,                  -- who owns this channel
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per live *session* of a stream. A new session is created on ingest start
-- and finalized on ingest stop; history is kept for archival + analytics.
CREATE TABLE IF NOT EXISTS stream_sessions (
    id           BIGSERIAL   PRIMARY KEY,
    stream_key   TEXT        NOT NULL REFERENCES streams(stream_key),

    -- Lifecycle state machine (V1). Legal transitions are enforced in code, not here.
    state        TEXT        NOT NULL DEFAULT 'ingesting', -- ingesting|transcoding|live|ended

    -- Which ingest node holds the RTMP/WebRTC connection. The lease guards against a
    -- node dying mid-stream: reconciliation ends sessions whose lease has expired.
    ingest_node  TEXT,
    lease_expires_at TIMESTAMPTZ,

    -- The ABR ladder this session is transcoded into (JSON array of renditions).
    ladder       JSONB       NOT NULL DEFAULT '[]',

    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at     TIMESTAMPTZ
);

-- At most one *active* (non-ended) session per stream key: an idempotent ingest-start
-- webhook must not create a second live session for a stream already live (V1).
CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_stream
    ON stream_sessions (stream_key)
    WHERE state <> 'ended';

-- The reconciler scans for live sessions and for expired leases on startup.
CREATE INDEX IF NOT EXISTS stream_sessions_state_idx ON stream_sessions (state);
