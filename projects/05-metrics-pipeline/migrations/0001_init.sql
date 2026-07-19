-- ClickHouse schema for the rolled-up metrics. NOTE: this is *not* a sqlx
-- migration (ClickHouse isn't Postgres) — it's plain DDL, mounted into the
-- container's /docker-entrypoint-initdb.d so a fresh volume applies it on first
-- start. Re-apply by hand with:
--   cat migrations/0001_init.sql | docker compose exec -T clickhouse clickhouse-client -mn

-- One row per (series, window): the output of the V2 rollup engine and the input
-- to the V3 sink. The pipeline writes here; `GET /query` reads here.
--
-- ReplacingMergeTree is the V3 idempotency lever: at-least-once delivery means a
-- crashed-then-redelivered batch re-inserts the SAME (series_id, window_start)
-- rows, and ReplacingMergeTree collapses duplicates on merge by the sort key. A
-- read that must see the deduped view uses `FINAL` (or aggregates over the key).
CREATE TABLE IF NOT EXISTS metrics_rollup
(
    series_id     UInt64,
    measurement   LowCardinality(String),  -- LowCardinality: few distinct names
    window_start  DateTime64(3, 'UTC'),
    window_secs   UInt32,

    count         UInt64,
    sum           Float64,
    min           Float64,
    max           Float64,

    -- Quantiles drawn from the V2 sketch when the window closed. See the TODO
    -- below: carrying the *sketch* (not just these numbers) is what lets you roll
    -- 1m windows up into 5m/1h correctly — you cannot average percentiles.
    p50           Float64,
    p99           Float64,

    -- Wall-clock insert time; handy for lag/debug, not part of identity.
    inserted_at   DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_at)   -- newest insert wins on a dup key
-- Read pattern is "this series, this time range", and old data ages out by time,
-- so partition by day and order by (series, time). The ORDER BY is also the
-- dedup key for ReplacingMergeTree — get it wrong and dupes won't collapse.
PARTITION BY toYYYYMMDD(window_start)
ORDER BY (series_id, window_start, window_secs)
-- TODO(V3, lifecycle): add a TTL to drop or roll up raw-resolution rollups after
-- a retention period, e.g. `TTL toDateTime(window_start) + INTERVAL 30 DAY`.
;

-- TODO(V2, multi-resolution): to serve "last 6 hours" without scanning every 1m
-- row, pre-roll 1m -> 5m -> 1h. The clean ClickHouse way is an AggregatingMerge-
-- Tree target + a MATERIALIZED VIEW that maintains it — and to merge percentiles
-- you must store a mergeable sketch state (e.g. `quantilesTDigestState`/
-- `AggregateFunction`), NOT the p50/p99 numbers above. Designing that rollup
-- ladder + sketch state column is the V2 storage-side lesson. Left out on
-- purpose — no spoilers.

-- TODO(V1, cardinality): you may also want a small `series` dimension table
-- (series_id -> measurement + tags JSON) so a query can resolve a fingerprint
-- back to human-readable tags, and so you can COUNT distinct series to enforce a
-- cardinality cap. Decide whether you need it once the query path is real.
