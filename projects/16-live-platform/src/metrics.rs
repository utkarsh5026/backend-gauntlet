//! Prometheus metrics for the observability checklist — **wired**, not a vertical.
//!
//! The [`metrics`] facade writes to a process-global recorder, so the call sites (in the
//! vertical modules) stay decoupled from this wiring — they just name a metric. Until
//! [`install`] sets a recorder the macros are no-ops, which is what unit tests want.
//!
//! [`install`] returns a [`PrometheusHandle`]; render it from `/metrics` (see [`crate::admin`]).
//! Two of these names matter beyond dashboards: [`TRANSCODE_QUEUE_DEPTH`] is what the HPA scales
//! the worker Deployment on (V2), and [`GLASS_TO_GLASS_MS`] is the latency the boss fight judges.

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

// --- control plane (gauges) ---
/// Streams currently live (packaged + playable).
pub const STREAMS_LIVE: &str = "live_streams";
/// Stream state transitions, labelled `to = ingesting|transcoding|live|ended`.
pub const STREAM_TRANSITIONS: &str = "live_stream_transitions_total";

// --- transcode workers (V2) ---
/// Transcode jobs waiting to be claimed. **This is the HPA autoscaling signal.**
pub const TRANSCODE_QUEUE_DEPTH: &str = "live_transcode_queue_depth";
/// Replicas the pool is asking HPA to scale to (derived from queue depth).
pub const TRANSCODE_DESIRED_REPLICAS: &str = "live_transcode_desired_replicas";
/// Transcode jobs finished, labelled `result = ok|retried|failed`.
pub const TRANSCODE_JOBS: &str = "live_transcode_jobs_total";

// --- edge delivery (V3) ---
/// Segment/playlist requests served, labelled `outcome = hit|miss|coalesced`.
pub const EDGE_REQUESTS: &str = "live_edge_requests_total";
/// Origin fills the edge issued on a miss (single-flight should keep this ≪ requests).
pub const EDGE_ORIGIN_FILLS: &str = "live_edge_origin_fills_total";

// --- chat (V4) ---
/// Chat WebSocket connections currently open (presence), labelled per node.
pub const CHAT_CONNECTIONS: &str = "live_chat_connections";
/// Chat messages fanned out to subscribers.
pub const CHAT_FANOUT: &str = "live_chat_fanout_total";
/// Chat subscribers dropped for lagging past the outbox (slow-consumer policy).
pub const CHAT_SLOW_DROPS: &str = "live_chat_slow_drops_total";

// --- end-to-end (histogram) ---
/// Glass-to-glass latency in milliseconds: capture timestamp → playable at the edge.
/// The number the boss fight targets.
pub const GLASS_TO_GLASS_MS: &str = "live_glass_to_glass_ms";

/// Install the process-global Prometheus recorder and return a handle used to render the
/// registry for `/metrics`. Call once, from `main`, after telemetry init.
pub fn install() -> PrometheusHandle {
    register_descriptions();
    PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder")
}

/// Register HELP metadata. Also gives rustc a direct use of each name constant (proc-macro
/// call sites alone don't always satisfy `dead_code`).
fn register_descriptions() {
    metrics::describe_gauge!(STREAMS_LIVE, "Streams currently live");
    metrics::describe_counter!(STREAM_TRANSITIONS, "Stream state transitions (to=…)");
    metrics::describe_gauge!(TRANSCODE_QUEUE_DEPTH, "Transcode jobs waiting (HPA signal)");
    metrics::describe_gauge!(TRANSCODE_DESIRED_REPLICAS, "Desired transcode replicas");
    metrics::describe_counter!(TRANSCODE_JOBS, "Transcode jobs finished (result=…)");
    metrics::describe_counter!(
        EDGE_REQUESTS,
        "Edge requests served (outcome=hit|miss|coalesced)"
    );
    metrics::describe_counter!(EDGE_ORIGIN_FILLS, "Origin fills issued on a miss");
    metrics::describe_gauge!(CHAT_CONNECTIONS, "Open chat WebSocket connections");
    metrics::describe_counter!(CHAT_FANOUT, "Chat messages fanned out");
    metrics::describe_counter!(CHAT_SLOW_DROPS, "Chat subscribers dropped for lagging");
    metrics::describe_histogram!(GLASS_TO_GLASS_MS, "Glass-to-glass latency (ms)");
}
