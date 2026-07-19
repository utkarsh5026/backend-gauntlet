//! Prometheus metrics for the observability checklist — **wired**, not a vertical.
//!
//! The [`metrics`] facade writes to a process-global recorder, so the call sites (in the vertical
//! modules) stay decoupled from this wiring — they just name a metric. Until [`install`] sets a
//! recorder the macros are no-ops, which is what unit tests want.
//!
//! [`install`] returns a [`PrometheusHandle`]; render it from `/metrics` (see [`crate::admin`]).
//! The name that matters beyond dashboards is [`RELAY_COPIES_OUT`]: it's the fan-out-amplification
//! number the boss fight judges — it must read **one copy per remote region per demanded layer**,
//! not one per subscriber.

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

// --- placement / consensus (V1) ---
/// Rooms currently placed (have a committed home region).
pub const ROOMS_PLACED: &str = "conf_rooms_placed";
/// Active regions for a room, summed across rooms (drives a `/status` gauge).
pub const ACTIVE_REGIONS: &str = "conf_active_regions";
/// Placement/membership entries committed through the replicated log.
pub const PLACEMENT_COMMITS: &str = "conf_placement_commits_total";
/// Leader elections started (a healthy mesh keeps this low).
pub const ELECTIONS: &str = "conf_elections_total";
/// This node's Raft-lite role as a gauge: 0 follower, 1 candidate, 2 leader.
pub const NODE_ROLE: &str = "conf_node_role";
/// This node's current term.
pub const NODE_TERM: &str = "conf_node_term";

// --- cascade transport (V2) ---
/// Backbone relay legs currently open, labelled by peer `region`.
pub const RELAY_LINKS: &str = "conf_relay_links";
/// **Relay copies sent out on the backbone**, labelled by destination `region`. The fan-out
/// amplification: this must be ~one per remote region per demanded layer, NOT one per subscriber.
pub const RELAY_COPIES_OUT: &str = "conf_relay_copies_out_total";
/// Relay copies received from a peer SFU (fanned out locally).
pub const RELAY_COPIES_IN: &str = "conf_relay_copies_in_total";
/// Bytes sent across the backbone.
pub const RELAY_BYTES_OUT: &str = "conf_relay_bytes_out_total";
/// Relay packets dropped, labelled `reason = loop|unknown_peer|no_route|truncated`.
pub const RELAY_DROPPED: &str = "conf_relay_dropped_total";

// --- cross-region routing (V3) ---
/// Simulcast layers currently carried on a backbone leg, labelled by peer `region`.
pub const LEG_LAYERS: &str = "conf_leg_layers";
/// Keyframe (PLI/FIR) requests sent upstream to a publisher on a region's up-switch.
pub const KEYFRAME_REQUESTS: &str = "conf_keyframe_requests_total";
/// Per-leg demand recomputations (a join/leave/layer-change that changed a leg's set).
pub const DEMAND_CHANGES: &str = "conf_demand_changes_total";

// --- recording (V4) ---
/// Recordings currently active (a recorder subscribed to a room).
pub const RECORDINGS_ACTIVE: &str = "conf_recordings_active";
/// Bytes of encoded RTP written to recordings.
pub const RECORDED_BYTES: &str = "conf_recorded_bytes_total";
/// Recording segments finalized (closed + indexed).
pub const RECORDING_SEGMENTS: &str = "conf_recording_segments_total";

// --- end-to-end (histogram) ---
/// Per-region forwarding latency (ms): ingress packet → egress `send_to`. Same p99 target as
/// project 15's SFU — the cascade must not inflate the in-region hop.
pub const FORWARD_LATENCY_MS: &str = "conf_forward_latency_ms";

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
    metrics::describe_gauge!(ROOMS_PLACED, "Rooms with a committed home region");
    metrics::describe_gauge!(ACTIVE_REGIONS, "Active regions summed across rooms");
    metrics::describe_counter!(PLACEMENT_COMMITS, "Placement/membership entries committed");
    metrics::describe_counter!(ELECTIONS, "Leader elections started");
    metrics::describe_gauge!(NODE_ROLE, "Node role: 0 follower, 1 candidate, 2 leader");
    metrics::describe_gauge!(NODE_TERM, "Current Raft-lite term");
    metrics::describe_gauge!(RELAY_LINKS, "Open backbone relay legs (by region)");
    metrics::describe_counter!(
        RELAY_COPIES_OUT,
        "Relay copies sent on the backbone (by region)"
    );
    metrics::describe_counter!(RELAY_COPIES_IN, "Relay copies received from peer SFUs");
    metrics::describe_counter!(RELAY_BYTES_OUT, "Bytes sent across the backbone");
    metrics::describe_counter!(RELAY_DROPPED, "Relay packets dropped (reason=…)");
    metrics::describe_gauge!(
        LEG_LAYERS,
        "Simulcast layers carried per backbone leg (by region)"
    );
    metrics::describe_counter!(
        KEYFRAME_REQUESTS,
        "Upstream keyframe requests on an up-switch"
    );
    metrics::describe_counter!(DEMAND_CHANGES, "Per-leg demand recomputations");
    metrics::describe_gauge!(RECORDINGS_ACTIVE, "Active recordings");
    metrics::describe_counter!(RECORDED_BYTES, "Encoded RTP bytes recorded");
    metrics::describe_counter!(RECORDING_SEGMENTS, "Recording segments finalized");
    metrics::describe_histogram!(FORWARD_LATENCY_MS, "Per-region forwarding latency (ms)");
}
