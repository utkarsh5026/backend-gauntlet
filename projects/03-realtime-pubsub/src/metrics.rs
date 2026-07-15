//! Prometheus metrics for the observability checklist (see SPEC.md → Horizontal
//! checklist → Observability).
//!
//! Same pattern as `01-url-shortener/src/metrics.rs`: the [`metrics`] facade
//! writes to a process-global recorder, so call sites elsewhere in the crate
//! (`hub.rs`, `backpressure.rs`, `routes.rs`, `presence.rs`) just name a metric
//! and stay decoupled from this wiring. Until [`install`] runs, the macros are
//! no-ops — exactly what tests want.
//!
//! [`install`] returns a [`PrometheusHandle`]; render it from `/metrics` (see
//! [`crate::routes::metrics_router`]) for a scrape endpoint.
//!
//! ## What's wired vs. what's a TODO
//! The recorder + `/metrics` endpoint are wired below. Drop counting on the
//! publish path lives in [`Mailbox::deliver`](crate::backpressure::Mailbox::deliver)
//! ([`MESSAGES_DROPPED_TOTAL`]). Remaining call sites are still **TODO**:
//! - `hub.rs::Hub::publish` — [`MESSAGES_DELIVERED_TOTAL`],
//!   [`SLOW_CLIENT_DISCONNECTS_TOTAL`] when a publish triggers disconnect cleanup.
//! - `routes.rs::dispatch` — increment [`MESSAGES_PUBLISHED_TOTAL`] when a client
//!   sends `Publish`.
//! - `routes.rs::handle_socket` — gauge [`OPEN_CONNECTIONS`] up on connect, down
//!   on teardown.
//! - `hub.rs` / subscribe paths — refresh [`SUBSCRIPTIONS_TOTAL`] and
//!   [`TOPICS_TOTAL`] gauges when membership changes (or sample from the hub map).
//! - `presence.rs` — gauge [`PRESENCE_MEMBERS`] per topic (or a single aggregate
//!   if you prefer fewer series).

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

/// Client-originated publishes accepted by the server. Increment in
/// `routes::dispatch` on `ClientMessage::Publish`.
pub const MESSAGES_PUBLISHED_TOTAL: &str = "realtime_pubsub_messages_published_total";

/// Fan-out deliveries that reached a subscriber's bounded mailbox (`DeliverOutcome::Delivered`).
/// Increment in `hub::Hub::publish` per subscriber reached.
pub const MESSAGES_DELIVERED_TOTAL: &str = "realtime_pubsub_messages_delivered_total";

/// Messages shed by the backpressure policy — the whole point of V2. Increment
/// on `DeliverOutcome::Dropped` and on `DropOldest` evictions. Label `policy`
/// with [`OverflowPolicyLabel`](OverflowPolicyLabel) (`drop_newest`, `drop_oldest`,
/// `disconnect`).
pub const MESSAGES_DROPPED_TOTAL: &str = "realtime_pubsub_messages_dropped_total";

/// Connections torn down because a full outbox triggered the `disconnect` policy
/// (or the mailbox was closed). Increment when `publish` reaps a wedged subscriber.
pub const SLOW_CLIENT_DISCONNECTS_TOTAL: &str = "realtime_pubsub_slow_client_disconnects_total";

/// Live WebSocket connections. Increment in `handle_socket` on connect, decrement
/// on every exit path.
pub const OPEN_CONNECTIONS: &str = "realtime_pubsub_open_connections";

/// Total active topic subscriptions across all connections (sum of hub map sizes).
pub const SUBSCRIPTIONS_TOTAL: &str = "realtime_pubsub_subscriptions_total";

/// Distinct topics with at least one subscriber.
pub const TOPICS_TOTAL: &str = "realtime_pubsub_topics_total";

/// Current room membership count. Consider a `topic` label for per-room visibility;
/// watch cardinality if topic names are client-controlled.
pub const PRESENCE_MEMBERS: &str = "realtime_pubsub_presence_members";

/// Install the process-global Prometheus recorder and return a handle used to
/// render the registry for `/metrics`. Call once, from `main`, after telemetry
/// init. Panics if a recorder is already installed (calling it twice is a bug).
pub fn install() -> PrometheusHandle {
    register_descriptions();
    PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder")
}

fn register_descriptions() {
    let published = MESSAGES_PUBLISHED_TOTAL;
    let delivered = MESSAGES_DELIVERED_TOTAL;
    let dropped = MESSAGES_DROPPED_TOTAL;
    let slow_disconnects = SLOW_CLIENT_DISCONNECTS_TOTAL;
    let open_connections = OPEN_CONNECTIONS;
    let subscriptions = SUBSCRIPTIONS_TOTAL;
    let topics = TOPICS_TOTAL;
    let presence = PRESENCE_MEMBERS;

    metrics::describe_counter!(
        published,
        "Client-originated publishes accepted by the server"
    );
    metrics::describe_counter!(
        delivered,
        "Fan-out deliveries that reached a subscriber mailbox"
    );
    metrics::describe_counter!(
        dropped,
        "Messages shed by the backpressure overflow policy, labelled by policy"
    );
    metrics::describe_counter!(
        slow_disconnects,
        "Connections disconnected because the outbound mailbox was full or closed"
    );
    metrics::describe_gauge!(open_connections, "Live WebSocket connections");
    metrics::describe_gauge!(
        subscriptions,
        "Active topic subscriptions across all connections"
    );
    metrics::describe_gauge!(topics, "Distinct topics with at least one subscriber");
    metrics::describe_gauge!(
        presence,
        "Current room membership count, optionally labelled by topic"
    );
}
