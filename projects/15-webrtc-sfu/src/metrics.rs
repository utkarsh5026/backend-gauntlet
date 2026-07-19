//! Prometheus metrics for the observability checklist.
//!
//! The [`metrics`] facade writes to a process-global recorder, so the counter/gauge call
//! sites (in [`pump`](crate::pump) and the wired [`sfu`](crate::sfu) core) stay decoupled
//! from this wiring — they just name a metric. Until [`install`] sets a recorder the macros
//! are no-ops, which is exactly what unit tests want (no setup, no panics).
//!
//! [`install`] returns a [`PrometheusHandle`]; render it from the `/metrics` route (see
//! [`crate::admin`]). The names below are the SFU's health signals: how much it forwards,
//! how it's adapting each subscriber, and whether it's requesting keyframes.

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

// --- topology (gauges) ---
/// Rooms currently active.
pub const ROOMS: &str = "sfu_rooms";
/// Peers connected, labelled `role = publisher|subscriber`.
pub const PEERS: &str = "sfu_peers";

// --- forwarding volume (counters) ---
/// RTP packets received from publishers (ingress).
pub const RTP_RECEIVED: &str = "sfu_rtp_received_total";
/// RTP packets forwarded to subscribers (egress) — one ingress packet fans out to many.
pub const RTP_FORWARDED: &str = "sfu_rtp_forwarded_total";
/// Media-plane bytes forwarded to subscribers.
pub const BYTES_FORWARDED: &str = "sfu_bytes_forwarded_total";
/// RTP packets dropped before forwarding, labelled `reason = no_route|not_selected|late`.
pub const RTP_DROPPED: &str = "sfu_rtp_dropped_total";

// --- ICE / STUN (counters) ---
/// STUN messages processed, labelled `kind = request|response|error`.
pub const STUN_MESSAGES: &str = "sfu_stun_messages_total";
/// ICE candidate pairs nominated (a peer became reachable).
pub const ICE_NOMINATED: &str = "sfu_ice_nominated_total";

// --- adaptation (counters) ---
/// Simulcast layer switches, labelled `dir = up|down`.
pub const LAYER_SWITCHES: &str = "sfu_layer_switches_total";
/// Keyframe requests (PLI/FIR) the SFU sent upstream to a publisher.
pub const KEYFRAME_REQUESTS: &str = "sfu_keyframe_requests_total";
/// NACKs forwarded/translated between a subscriber and the origin publisher.
pub const NACKS_TRANSLATED: &str = "sfu_nacks_translated_total";

// --- quality (gauges) ---
/// Estimated available downlink bitrate for the busiest subscriber, bits/sec.
pub const ESTIMATED_BITRATE: &str = "sfu_estimated_bitrate_bps";
/// Forwarded bitrate currently selected for the busiest subscriber, bits/sec.
pub const SELECTED_BITRATE: &str = "sfu_selected_bitrate_bps";

/// Install the process-global Prometheus recorder and return a handle used to render the
/// registry for `/metrics`. Call once, from `main`, after telemetry init. Panics if a
/// recorder is already installed (calling it twice is a bug).
pub fn install() -> PrometheusHandle {
    register_descriptions();
    PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder")
}

/// Register HELP metadata for the metrics. Call sites only pass the name constant;
/// describing them here keeps the strings single-sourced and gives rustc a direct use of
/// each constant (proc-macro call sites alone do not always satisfy `dead_code`).
fn register_descriptions() {
    metrics::describe_gauge!(ROOMS, "Active rooms");
    metrics::describe_gauge!(PEERS, "Connected peers (role=publisher|subscriber)");
    metrics::describe_counter!(RTP_RECEIVED, "RTP packets received from publishers");
    metrics::describe_counter!(RTP_FORWARDED, "RTP packets forwarded to subscribers");
    metrics::describe_counter!(BYTES_FORWARDED, "Media-plane bytes forwarded");
    metrics::describe_counter!(
        RTP_DROPPED,
        "RTP packets dropped (reason=no_route|not_selected|late)"
    );
    metrics::describe_counter!(
        STUN_MESSAGES,
        "STUN messages processed (kind=request|response|error)"
    );
    metrics::describe_counter!(ICE_NOMINATED, "ICE candidate pairs nominated");
    metrics::describe_counter!(LAYER_SWITCHES, "Simulcast layer switches (dir=up|down)");
    metrics::describe_counter!(
        KEYFRAME_REQUESTS,
        "Keyframe (PLI/FIR) requests sent upstream"
    );
    metrics::describe_counter!(NACKS_TRANSLATED, "NACKs translated subscriber<->publisher");
    metrics::describe_gauge!(ESTIMATED_BITRATE, "Estimated downlink bitrate (bps)");
    metrics::describe_gauge!(SELECTED_BITRATE, "Selected forwarded bitrate (bps)");
}
