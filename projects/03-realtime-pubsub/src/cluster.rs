//! V4 — Multi-node fan-out: one logical topic across many processes.
//!
//! A [`Hub`](crate::hub::Hub) only knows about *this* node's sockets. With two
//! nodes behind a load balancer, a publish on node A never reaches a subscriber
//! whose socket lives on node B. The bridge closes that gap by carrying messages
//! over a **cross-node bus** (Redis pub/sub):
//!
//! ```text
//!   client→A  ──publish──▶  A.hub (local sockets)
//!                           └──▶ Redis channel ──▶ B.run() ──▶ B.hub (local sockets)
//! ```
//!
//! Two rules make or break it:
//!   1. The receive side injects into the **local hub only** — it must NOT
//!      re-publish to Redis, or every message loops forever.
//!   2. Each message is stamped with this node's id, so a node recognises and
//!      drops its own messages coming back around the bus.

use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::hub::Hub;
use crate::protocol::ServerMessage;

/// What travels over the bus: the broadcast plus the id of the node that
/// originated it (for loop-breaking / de-dup on the receive side).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BusEnvelope {
    /// `NODE_ID` of the node that first published this. Receivers drop their own.
    pub origin: String,
    pub topic: String,
    pub payload: serde_json::Value,
}

/// Bridges the local [`Hub`] to Redis pub/sub. Held in `AppState` as
/// `Option<Arc<ClusterBridge>>` — `None` is single-node mode (V1–V3), where the
/// bus is never touched.
pub struct ClusterBridge {
    /// This node's stable id (`NODE_ID`). Stamped onto every outgoing envelope.
    node_id: String,
    /// Redis handle. `open` doesn't connect; the actual connections are made in
    /// `publish` (to send) and `run` (to subscribe).
    client: redis::Client,
    /// The local hub the receive loop injects bus messages into.
    hub: Arc<Hub>,
}

impl ClusterBridge {
    /// Wire the bridge. Cheap and synchronous — `Client::open` only parses the
    /// URL; no socket is opened until `publish` / `run`.
    pub fn connect(
        redis_url: &str,
        node_id: String,
        hub: Arc<Hub>,
    ) -> Result<Self, redis::RedisError> {
        let client = redis::Client::open(redis_url)?;
        Ok(Self {
            node_id,
            client,
            hub,
        })
    }

    /// Put a broadcast onto the bus so other nodes can deliver it locally.
    pub async fn publish(&self, topic: &str, payload: &serde_json::Value) {
        // TODO(V4): publish a `BusEnvelope { origin: self.node_id, topic, payload }`
        // (JSON-encoded) to a Redis channel for this topic, e.g. `pubsub:{topic}`.
        //   - Get an async connection (a cached `ConnectionManager` beats opening
        //     one per publish on the hot path).
        //   - This is fire-and-forget-ish: log and swallow bus errors so a Redis
        //     hiccup degrades to single-node delivery rather than failing the
        //     client's publish.
        let _ = (topic, payload, &self.node_id, &self.client, &self.hub);
        todo!("V4: publish a broadcast onto the Redis bus")
    }

    /// Background task: subscribe to the bus and inject arriving messages into the
    /// local hub. Spawn this once at startup; it runs for the process lifetime.
    pub async fn run(self: Arc<Self>) {
        // TODO(V4): the receive side of the bridge.
        //   1. Open a Redis pub/sub connection and (P)SUBSCRIBE to the topic
        //      channels — `PSUBSCRIBE pubsub:*` is the simple start; subscribing
        //      lazily per active topic is the scalable version (see SPEC).
        //   2. For each message: decode the `BusEnvelope`.
        //   3. LOOP-BREAK: if `envelope.origin == self.node_id`, drop it — this is
        //      our own message echoing back; we already delivered it locally.
        //   4. Otherwise inject into the LOCAL hub only:
        //      `self.hub.publish(&topic, ServerMessage::Message { topic, payload })`.
        //      Do NOT call `self.publish(...)` here — that re-emits to the bus and
        //      builds an infinite echo loop.
        let _: fn(String, serde_json::Value) -> ServerMessage =
            |topic, payload| ServerMessage::Message { topic, payload }; // keep import live
        let _ = (&self.node_id, &self.client, &self.hub);
        todo!("V4: subscribe to the bus and inject into the local hub")
    }
}
