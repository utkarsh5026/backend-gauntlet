//! Prometheus metrics for the observability checklist.
//!
//! The [`metrics`] facade writes to a process-global recorder, so the call sites (in
//! [`server`](crate::server), [`engine`](crate::engine), [`wal`](crate::wal),
//! [`compaction`](crate::compaction)) name a metric without knowing about this wiring.
//! Until [`install`] sets a recorder the macros are no-ops — exactly what unit tests
//! want (no setup, no panics).
//!
//! [`install`] returns a [`PrometheusHandle`]; render it from `/metrics` (see
//! [`crate::routes::metrics_router`]). The series the SPEC + boss fight grade:
//! - [`COMMANDS_TOTAL`] — commands served, labelled `cmd` (the ops/sec source).
//! - [`COMMAND_DURATION`] — end-to-end command latency histogram (source of p99).
//! - [`MEMTABLE_BYTES`] — active memtable size (the flush-trigger signal).
//! - [`SSTABLES`] — live SSTable count, labelled `level` (the write-stall backlog signal).
//! - [`COMPACTIONS_TOTAL`] / [`COMPACTION_BYTES_TOTAL`] — compaction progress (V6).
//! - [`BLOCK_CACHE_LOOKUPS_TOTAL`] — labelled `outcome = hit|miss`; `hit / sum` is the ratio.
//! - [`WAL_FSYNC_DURATION`] — fsync latency histogram (the durability/throughput dial, V2).
//! - [`CONNECTED_CLIENTS`] — currently-open RESP connections.
//!
//! Wiring the *call sites* is the observability horizontal item — this module just
//! makes `/metrics` render and single-sources the metric names.

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

/// Commands served, labelled `cmd = get|set|del|…`. Rate of this is ops/sec.
pub const COMMANDS_TOTAL: &str = "lsm_commands_total";

/// End-to-end command latency, in seconds — rendered as quantiles, so p99 falls out.
pub const COMMAND_DURATION: &str = "lsm_command_duration_seconds";

/// Active memtable size in bytes — climbs on writes, drops to ~0 on a flush.
pub const MEMTABLE_BYTES: &str = "lsm_memtable_bytes";

/// Live SSTables, labelled by `level`. A youngest-level count that keeps climbing is
/// the compaction-can't-keep-up (write-stall) signal the boss fight watches.
pub const SSTABLES: &str = "lsm_sstables";

/// Compactions completed (V6).
pub const COMPACTIONS_TOTAL: &str = "lsm_compactions_total";

/// Bytes rewritten by compaction — the write-amplification meter.
pub const COMPACTION_BYTES_TOTAL: &str = "lsm_compaction_bytes_total";

/// Block-cache lookups, labelled `outcome = hit|miss`. Hit ratio = `hit / (hit+miss)`.
pub const BLOCK_CACHE_LOOKUPS_TOTAL: &str = "lsm_block_cache_lookups_total";

/// WAL fsync latency, seconds — the cost the durability policy (V2) is trading against.
pub const WAL_FSYNC_DURATION: &str = "lsm_wal_fsync_duration_seconds";

/// Currently-open RESP connections.
pub const CONNECTED_CLIENTS: &str = "lsm_connected_clients";

/// Install the process-global Prometheus recorder and return a handle used to render
/// the registry for `/metrics`. Call once, from `main`, after telemetry init. Panics
/// if a recorder is already installed (calling it twice is a bug).
pub fn install() -> PrometheusHandle {
    register_descriptions();
    PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder")
}

/// Register HELP metadata for the graded metrics. Naming each constant here also gives
/// rustc a direct use of it (proc-macro call sites alone don't always satisfy
/// `dead_code`), so the scaffold stays warning-quiet before the call sites exist.
fn register_descriptions() {
    metrics::describe_counter!(COMMANDS_TOTAL, "Commands served, labelled by cmd");
    metrics::describe_histogram!(COMMAND_DURATION, "End-to-end command latency, seconds");
    metrics::describe_gauge!(MEMTABLE_BYTES, "Active memtable size in bytes");
    metrics::describe_gauge!(SSTABLES, "Live SSTables, labelled by level");
    metrics::describe_counter!(COMPACTIONS_TOTAL, "Compactions completed");
    metrics::describe_counter!(COMPACTION_BYTES_TOTAL, "Bytes rewritten by compaction");
    metrics::describe_counter!(
        BLOCK_CACHE_LOOKUPS_TOTAL,
        "Block-cache lookups, labelled hit|miss"
    );
    metrics::describe_histogram!(WAL_FSYNC_DURATION, "WAL fsync latency, seconds");
    metrics::describe_gauge!(CONNECTED_CLIENTS, "Currently-open RESP connections");
}
