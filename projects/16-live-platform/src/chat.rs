//! V4 — Chat & presence fan-out at scale. `src/chat.rs`.
//!
//! Every live channel has a chat, and a viral stream can put 100k people in one room. This is
//! project 03's WebSocket fan-out, now multi-tenant and pushed hard, so the failure modes that
//! matter are about **isolation and backpressure**:
//!
//! 1. **Per-channel fan-out, sharded.** One `broadcast` channel per stream so a message posted
//!    in channel A reaches A's viewers only. A firehose channel must not stall a quiet one.
//! 2. **Slow-consumer handling.** A viewer on hotel wifi can't be allowed to back-pressure the
//!    broadcaster: each subscriber has a bounded outbox and an explicit overflow policy (lag →
//!    drop/disconnect), never unbounded buffering. (The `broadcast` receiver's `Lagged` is the
//!    signal.)
//! 3. **Presence + cross-node bus.** Viewer counts per channel, and — because the platform runs
//!    as many pods — a message published on one node must reach subscribers on the others via
//!    Redis pub/sub (project 03 V4), with each node dropping its own echoes.
//!
//! Scaffold state: the hub is constructed with an outbox capacity + a Redis handle and can
//! report (empty) presence; join/publish and the cross-node bridge are the V4 `todo!()` worklist.

use std::collections::HashMap;
use std::sync::RwLock;

use serde::{Deserialize, Serialize};
use tokio::sync::broadcast;

use crate::error::Result;

/// One chat message fanned out to a channel's subscribers.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ChatMessage {
    pub stream_key: String,
    /// Display name of the sender (already resolved/authorized upstream).
    pub user: String,
    pub body: String,
    pub sent_at_ms: i64,
}

/// A live channel's fan-out: the broadcast sender every subscriber clones a receiver from.
struct Channel {
    tx: broadcast::Sender<ChatMessage>,
}

/// The chat hub: one [`Channel`] per live stream, plus the cross-node Redis bus.
pub struct ChatHub {
    /// Per-subscriber outbox capacity — how far a slow viewer may lag before the
    /// broadcast channel drops messages for them (the overflow policy's threshold).
    outbox_capacity: usize,
    /// Stable id for THIS pod, stamped on outbound bus messages so a node drops its own echoes.
    node_id: String,
    /// Cross-node connection manager (Redis pub/sub) for multi-pod fan-out.
    redis: redis::aio::ConnectionManager,
    channels: RwLock<HashMap<String, Channel>>,
}

impl ChatHub {
    /// Build the hub. Wiring only — no channels exist until the first join.
    pub fn new(
        outbox_capacity: usize,
        node_id: String,
        redis: redis::aio::ConnectionManager,
    ) -> Self {
        Self {
            outbox_capacity,
            node_id,
            redis,
            channels: RwLock::new(HashMap::new()),
        }
    }

    /// Number of channels with at least one subscriber (a `/status` gauge).
    pub fn active_channels(&self) -> usize {
        self.channels.read().expect("channels lock").len()
    }

    /// Local subscriber count for a channel — the presence number for this pod. (The
    /// cluster-wide count sums this across pods over the bus; that's part of V4.)
    pub fn local_presence(&self, stream_key: &str) -> usize {
        self.channels
            .read()
            .expect("channels lock")
            .get(stream_key)
            .map(|c| c.tx.receiver_count())
            .unwrap_or(0)
    }

    // ---- V4 worklist: fan-out, backpressure, presence, cross-node bus -------------

    /// TODO(V4): Subscribe to a channel's chat. Create the channel's broadcast sender on
    /// first join (bounded by `outbox_capacity`), and return a receiver. The WebSocket task
    /// (see `routes`) reads this receiver and must handle `RecvError::Lagged` per the overflow
    /// policy — a slow viewer is dropped, never allowed to stall the broadcaster.
    pub fn join(&self, stream_key: &str) -> Result<broadcast::Receiver<ChatMessage>> {
        let _ = (stream_key, self.outbox_capacity);
        todo!("V4: get-or-create the channel, return a bounded broadcast receiver")
    }

    /// TODO(V4): Publish a message to a channel — fan out to *local* subscribers and forward
    /// onto the Redis bus so subscribers on *other* pods get it too. Stamp `node_id` so the
    /// receiving side drops this pod's own echo.
    pub async fn publish(&self, msg: ChatMessage) -> Result<()> {
        let _ = (msg, &self.node_id, &self.redis);
        todo!("V4: local broadcast + publish to Redis for cross-node fan-out")
    }

    /// TODO(V4): The cross-node bridge. Subscribe to the Redis channel and re-broadcast
    /// messages from *other* pods into local channels, dropping any this pod originated
    /// (matched by `node_id`). Runs as a background task started in `main`.
    pub async fn run_bus(&self) -> Result<()> {
        todo!("V4: consume the Redis pub/sub bus, re-broadcast remote messages locally")
    }
}
