//! The wire protocol and connection identity — the glue types every module
//! speaks. This is scaffolding, not a vertical challenge: the message *shapes*
//! are given so the WebSocket handler, the hub, and the cluster bridge all agree
//! on a vocabulary. Extend it (e.g. add `ping`/`pong` app-level frames, an ack,
//! or a `history` request) as the SPEC's protocol checklist asks.

use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};

pub type Topic = String;

/// A process-unique id for one open WebSocket connection. Cheap to copy; used as
/// the key a subscriber is tracked under in the hub and presence registry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ConnId(u64);

impl ConnId {
    /// Mint the next id. Monotonic for the lifetime of the process.
    pub fn next() -> Self {
        static COUNTER: AtomicU64 = AtomicU64::new(1);
        ConnId(COUNTER.fetch_add(1, Ordering::Relaxed))
    }

    pub fn as_u64(self) -> u64 {
        self.0
    }
}

impl std::fmt::Display for ConnId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "conn-{}", self.0)
    }
}

/// Messages a client sends *to* the server (deserialized from each text frame).
///
/// The `type` field is the tag, e.g. `{"type":"subscribe","topic":"room1"}`.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientMessage {
    /// Join a topic; future publishes to it will be delivered to this socket.
    Subscribe { topic: String },
    /// Leave a topic.
    Unsubscribe { topic: String },
    /// Broadcast `payload` to everyone subscribed to `topic`.
    Publish {
        topic: String,
        payload: serde_json::Value,
    },
    /// Application-level liveness ping (`{"type":"heartbeat"}`). Browsers can't
    /// send WebSocket protocol ping frames from JS, so a live-but-idle client
    /// sends this on a timer to prove it's still here and refresh its presence
    /// TTL. Carries no payload — its arrival *is* the signal.
    Heartbeat,
}

/// Messages the server sends *to* a client (serialized into a text frame).
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerMessage {
    /// A broadcast delivered on a topic this client subscribes to.
    Message {
        topic: String,
        payload: serde_json::Value,
    },
    /// The current membership of a topic (V3).
    Presence { topic: String, members: Vec<String> },
    /// A protocol-level error (bad frame, unknown topic, over a limit, …).
    Error { reason: String },
}
