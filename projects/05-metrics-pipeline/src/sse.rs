//! V4 — The SSE live fan-out: push closed windows to many dashboards.
//!
//! Every time the rollup engine closes a window (V2), that row is broadcast to
//! every connected `GET /stream` client over **Server-Sent Events**. SSE — not
//! WebSocket — is the right tool: the traffic is one-directional (server→client),
//! it rides plain HTTP, and browsers auto-reconnect for free.
//!
//! The lesson (SPEC V4) is the *inverse* of V3's backpressure. The durable sink
//! must never drop a rollup. The live view **must be willing to drop**: a slow
//! browser tab cannot be allowed to back-pressure the pipeline. So the hub is a
//! bounded [`broadcast`] channel and a lagging subscriber is shed (its receiver
//! reports `Lagged`), never letting it stall the producer.

use tokio::sync::broadcast;

use axum::response::Response;

use crate::model::RollupRow;

/// The fan-out hub: one sender, many SSE subscribers.
///
/// `publish` is called by the pipeline as windows close (V2→V4 hand-off);
/// `subscribe` hands each connected client its own receiver. The channel is
/// **bounded** on purpose — that bound is the load-shedding policy.
#[derive(Clone)]
pub struct LiveFeed {
    tx: broadcast::Sender<RollupRow>,
}

impl LiveFeed {
    /// `capacity` is how many rollups a subscriber may fall behind before it's
    /// marked lagged and starts dropping — the size of the "we may drop for live
    /// views" window. Tune it against your window-close rate.
    pub fn new(capacity: usize) -> Self {
        let (tx, _rx) = broadcast::channel(capacity);
        Self { tx }
    }

    /// Broadcast a closed window to all subscribers. Deliberately ignores "no
    /// receivers" — an idle dashboard must not affect the pipeline.
    pub fn publish(&self, row: RollupRow) {
        let _ = self.tx.send(row);
    }

    /// A fresh receiver for one connected client.
    pub fn subscribe(&self) -> broadcast::Receiver<RollupRow> {
        self.tx.subscribe()
    }

    /// Current subscriber count — export as the connected-clients gauge.
    pub fn subscribers(&self) -> usize {
        self.tx.receiver_count()
    }
}

/// Build the SSE response for one `GET /stream` connection (V4).
///
/// `last_event_id` is the browser's `Last-Event-ID` header on reconnect — use it
/// to resume without gaps where you can.
pub async fn stream(feed: &LiveFeed, last_event_id: Option<String>) -> Response {
    // TODO(V4): build and return an `axum::response::sse::Sse` response.
    //   - turn `feed.subscribe()` into a `Stream` of `Result<Event, _>`
    //     (`tokio_stream::wrappers::BroadcastStream` does this); map each
    //     `RollupRow` to `Event::default().json_data(row)?` with an `.id(...)`.
    //   - handle the slow-client case: a `BroadcastStream` yields
    //     `Err(Lagged(n))` when a subscriber fell behind — SHED it (skip and
    //     count, maybe send a `event: lag` notice), never block. This is the
    //     "live views may drop" half of the backpressure lesson.
    //   - set `Sse::keep_alive(...)` so idle connections and proxies don't time
    //     the stream out, and emit a `retry:` so a dropped client reconnects.
    //   - honour `last_event_id` for resume where feasible.
    let _ = (feed, last_event_id);
    todo!("V4: stream closed rollup windows to one client over SSE")
}
