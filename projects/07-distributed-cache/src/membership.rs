//! V3 — Gossip membership & failure detection (SWIM).
//!
//! This is the layer you'd normally get from a service registry (ZooKeeper,
//! etcd, Consul). Here the nodes agree on who's alive *among themselves*, with no
//! central authority, using SWIM:
//!
//!   - **Failure detection:** each round, ping one random peer. No ack? Ask `k`
//!     other peers to ping it *for* you (indirect probe) before you suspect it —
//!     so one dropped packet doesn't evict a healthy node.
//!   - **Dissemination:** piggyback membership updates on those pings (gossip),
//!     so news of a join/death spreads in O(log n) rounds with constant per-node
//!     message load — not the O(n²) of all-to-all heartbeating.
//!   - **Suspicion + incarnation:** a suspected node gets a grace window to
//!     *refute* (bump its incarnation number) before it's declared dead, killing
//!     the flapping you'd get from a naive timeout.
//!
//! This module owns the authoritative member list **and the ring (V2)** — a
//! membership change is exactly when the ring must be rebuilt, so they live
//! together behind one lock. The coordinator (V4) reads this to route requests.
//!
//! Scaffold state: the node binds its UDP socket, seeds itself into its own
//! member list, and spawns a receive loop — so `GET /cluster` shows this node.
//! The gossip *protocol* (probing, suspicion, applying updates) is `todo!()`.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::RwLock;

use serde::{Deserialize, Serialize};
use tokio::net::UdpSocket;
use tracing::info;

use crate::node::{Node, NodeId};
use crate::ring::Ring;

/// Where a member sits in the SWIM lifecycle. `Alive → Suspect → Dead`, with a
/// refutation path back to `Alive` via a higher incarnation number.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MemberState {
    Alive,
    Suspect,
    Dead,
}

/// One node in this node's view of the cluster.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Member {
    pub node: Node,
    pub state: MemberState,
    /// Monotonic per-node counter. A node refutes a false `Suspect` by
    /// re-broadcasting itself `Alive` at a higher incarnation; peers keep the
    /// highest incarnation they've seen, so stale gossip can't resurrect or
    /// re-kill a node. This is the anti-flapping mechanism.
    pub incarnation: u64,
}

/// A single membership fact, piggybacked on gossip messages so news spreads
/// without dedicated broadcast traffic.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MemberUpdate {
    pub node: Node,
    pub state: MemberState,
    pub incarnation: u64,
}

/// The SWIM wire protocol (UDP datagrams, serialized as JSON for legibility while
/// you build it — a binary codec is a later optimisation). Every message carries
/// a batch of `updates` to gossip; that piggybacking is the dissemination path.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum GossipMessage {
    /// Sent to a seed on startup to enter the cluster.
    Join { node: Node },
    /// Direct failure-detection probe.
    Ping {
        from: NodeId,
        updates: Vec<MemberUpdate>,
    },
    /// Reply proving liveness.
    Ack {
        from: NodeId,
        updates: Vec<MemberUpdate>,
    },
    /// "Please ping `target` for me and relay the ack" — the indirect probe that
    /// prevents a single lost packet from evicting a healthy node.
    PingReq {
        from: NodeId,
        target: NodeId,
        updates: Vec<MemberUpdate>,
    },
}

/// The mutable cluster view: the member table and the hash ring derived from it.
/// They change together — every membership transition rebuilds the ring so
/// ownership always follows the live set.
struct View {
    members: HashMap<NodeId, Member>,
    ring: Ring,
}

/// Membership service: owns the view, the UDP socket, and (once you build it) the
/// gossip driver. Shared as `Arc<Membership>` by the gossip task and the
/// coordinator.
pub struct Membership {
    self_id: NodeId,
    seeds: Vec<SocketAddr>,
    socket: UdpSocket,
    view: RwLock<View>,
}

impl Membership {
    /// Bind the gossip UDP socket and seed the view with *this* node (Alive).
    /// Note it does **not** yet place the node on the ring — see the TODO: wiring
    /// membership → ring is part of V3, and until `Ring::add_node` (V2) exists
    /// there's nothing to add.
    pub async fn bind(
        self_node: Node,
        seeds: Vec<SocketAddr>,
        vnodes_per_node: u32,
    ) -> anyhow::Result<std::sync::Arc<Self>> {
        let socket = UdpSocket::bind(self_node.gossip_addr).await?;
        info!(gossip_addr = %self_node.gossip_addr, "gossip socket bound");

        let mut members = HashMap::new();
        members.insert(
            self_node.id.clone(),
            Member {
                node: self_node.clone(),
                state: MemberState::Alive,
                incarnation: 0,
            },
        );

        let ring = Ring::new(vnodes_per_node);
        // TODO(V3): seed the ring with self — `ring.add_node(&self_node.id)` —
        // once V2's `add_node` is implemented, and call it again whenever a peer
        // transitions to/from Alive so ownership tracks the live set.

        Ok(std::sync::Arc::new(Self {
            self_id: self_node.id.clone(),
            seeds,
            socket,
            view: RwLock::new(View { members, ring }),
        }))
    }

    pub fn self_id(&self) -> &NodeId {
        &self.self_id
    }

    /// The `n` replica node ids for a key, read off the current ring (V2). The
    /// coordinator (V4) turns these into addresses and filters the dead.
    pub fn replicas(&self, key: &str, n: usize) -> Vec<NodeId> {
        self.view.read().unwrap().ring.replicas(key, n)
    }

    /// Resolve a node id to its reachable address, if we know it.
    pub fn resolve(&self, id: &NodeId) -> Option<Node> {
        self.view
            .read()
            .unwrap()
            .members
            .get(id)
            .map(|m| m.node.clone())
    }

    /// Is this node currently believed alive? (Coordinator skips dead replicas.)
    pub fn is_alive(&self, id: &NodeId) -> bool {
        self.view
            .read()
            .unwrap()
            .members
            .get(id)
            .is_some_and(|m| m.state == MemberState::Alive)
    }

    /// A snapshot of the whole membership view — backs `GET /cluster` and the
    /// membership-size metric. Real even before gossip works (shows this node).
    pub fn snapshot(&self) -> Vec<Member> {
        self.view
            .read()
            .unwrap()
            .members
            .values()
            .cloned()
            .collect()
    }

    /// Drive SWIM forever: a receive loop + (TODO) a probe ticker. Spawned from
    /// `main`. Blocks on `recv_from`, so an idle node is quiet — it only comes
    /// alive once you send the first `Join`/`Ping`.
    pub async fn run(self: std::sync::Arc<Self>) {
        // TODO(V3): the two concurrent halves of SWIM. Run them with
        // `tokio::select!` (or spawn a second task):
        //   1. RECEIVE loop (below): recv_from → deserialize GossipMessage →
        //      handle_message → apply piggybacked updates.
        //   2. PROBE ticker: every PROBE_INTERVAL, pick a *random* alive member
        //      (that's what `rand` is for), Ping it; on no Ack, PingReq via `k`
        //      others; still nothing → mark Suspect; after the suspicion timeout
        //      → Dead. On any membership change, rebuild the ring.
        // Also: on startup, send `Join` to each seed so this node enters the
        // cluster.
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            match self.socket.recv_from(&mut buf).await {
                Ok((len, from)) => {
                    if let Err(e) = self.handle_datagram(&buf[..len], from).await {
                        tracing::warn!(error = %e, %from, "bad gossip datagram");
                    }
                }
                Err(e) => {
                    tracing::warn!(error = %e, "gossip recv failed; stopping gossip loop");
                    break;
                }
            }
        }
    }

    /// Decode one datagram and act on it. The decode is plumbing; the *acting*
    /// (state transitions, replying, merging updates) is the V3 learning.
    async fn handle_datagram(&self, bytes: &[u8], from: SocketAddr) -> anyhow::Result<()> {
        let msg: GossipMessage = serde_json::from_slice(bytes)?;
        // TODO(V3): dispatch on `msg`:
        //   - Join   → add the joiner (Alive), reply/gossip it onward;
        //   - Ping   → apply `updates`, reply Ack (piggybacking your own updates);
        //   - Ack    → apply `updates`, mark the prober's probe satisfied;
        //   - PingReq→ ping `target`; relay its Ack back to `from`.
        // Merging an update = keep the entry with the higher incarnation, and let
        // the local node refute a Suspect about itself by bumping its incarnation.
        // Any transition that changes the live set must rebuild the ring.
        let _ = (msg, from);
        todo!("V3: handle a decoded gossip message and merge its updates")
    }
}
