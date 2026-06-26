-- Links table. The slug is the public short code; id is the Snowflake id (V1).
-- NOTE: we store id as BIGINT (i64) generated in-process, NOT a serial/sequence —
-- that's the whole point of the V1 challenge.
CREATE TABLE IF NOT EXISTS links (
    id           BIGINT PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    long_url     TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_links_slug ON links (slug);

-- Raw click events, written by the async ingestion task (V3) in batches.
-- This table grows fast; in Tier 3 you'll learn why you'd push this to a
-- columnar store (ClickHouse) instead of Postgres.
CREATE TABLE IF NOT EXISTS click_events (
    id          BIGSERIAL PRIMARY KEY,
    link_id     BIGINT NOT NULL REFERENCES links(id),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    referer     TEXT,
    user_agent  TEXT,
    ip_hash     TEXT
);

CREATE INDEX IF NOT EXISTS idx_click_events_link_id ON click_events (link_id);
