//! Prometheus metrics for the observability checklist.
//!
//! The [`metrics`] facade writes to a process-global recorder, so the counter/gauge call
//! sites (in [`session`](crate::session)) stay decoupled from this wiring — they just name
//! a metric. Until [`install`] sets a recorder the macros are no-ops, which is exactly what
//! tests want (no metrics setup, no panics).
//!
//! [`install`] returns a [`PrometheusHandle`]; render it from the `/metrics` route
//! (see [`crate::admin`]). The names below cover the transport's quality signals — the
//! ones you watch to see the picture degrade before an eye does.

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

// --- volume (counters) ---
/// RTP packets sent, labelled `kind = original|retransmit`.
pub const PACKETS_SENT: &str = "media_transport_packets_sent_total";
/// RTP packets received (before jitter-buffer admission).
pub const PACKETS_RECEIVED: &str = "media_transport_packets_received_total";
/// Media-plane bytes sent.
pub const BYTES_SENT: &str = "media_transport_bytes_sent_total";
/// Media-plane bytes received.
pub const BYTES_RECEIVED: &str = "media_transport_bytes_received_total";

// --- loss & recovery (counters) ---
/// Packets detected missing at the receiver (gaps in the sequence).
pub const PACKETS_LOST: &str = "media_transport_packets_lost_total";
/// NACK feedback packets, labelled `dir = sent|received`.
pub const NACKS_TOTAL: &str = "media_transport_nacks_total";
/// Retransmitted packets that were actually resent from the history cache.
pub const RETRANSMITS: &str = "media_transport_retransmits_total";
/// Duplicate packets discarded by the jitter buffer.
pub const DUPLICATES: &str = "media_transport_duplicates_total";
/// Packets dropped for arriving after their playout deadline.
pub const LATE_DROPS: &str = "media_transport_late_drops_total";

// --- quality (gauges) ---
/// Smoothed interarrival jitter estimate, in milliseconds (RFC 3550).
pub const JITTER_MS: &str = "media_transport_jitter_ms";
/// Current jitter-buffer depth, in packets.
pub const JITTER_BUFFER_DEPTH: &str = "media_transport_jitter_buffer_depth";
/// Congestion-control target send rate, in bits/sec.
pub const TARGET_BITRATE: &str = "media_transport_target_bitrate_bps";

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
    metrics::describe_counter!(PACKETS_SENT, "RTP packets sent (kind=original|retransmit)");
    metrics::describe_counter!(PACKETS_RECEIVED, "RTP packets received");
    metrics::describe_counter!(BYTES_SENT, "Media-plane bytes sent");
    metrics::describe_counter!(BYTES_RECEIVED, "Media-plane bytes received");
    metrics::describe_counter!(PACKETS_LOST, "Packets detected missing at the receiver");
    metrics::describe_counter!(NACKS_TOTAL, "NACK feedback packets (dir=sent|received)");
    metrics::describe_counter!(RETRANSMITS, "Packets resent from the retransmit cache");
    metrics::describe_counter!(DUPLICATES, "Duplicate packets discarded");
    metrics::describe_counter!(LATE_DROPS, "Packets dropped for missing their deadline");
    metrics::describe_gauge!(JITTER_MS, "Smoothed interarrival jitter (ms)");
    metrics::describe_gauge!(JITTER_BUFFER_DEPTH, "Jitter-buffer depth (packets)");
    metrics::describe_gauge!(TARGET_BITRATE, "Congestion-control target bitrate (bps)");
}
