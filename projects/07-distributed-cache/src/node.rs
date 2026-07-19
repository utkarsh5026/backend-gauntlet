//! Shared identity types used across the ring (V2), membership (V3), and
//! coordinator (V4). Not a vertical of its own — just the vocabulary the
//! interesting modules agree on.

use std::net::SocketAddr;

use serde::{Deserialize, Serialize};

/// A stable, human-readable node identity (from the `NODE_ID` env var, e.g.
/// `cache-a`). The ring hashes *this* string to place a node's virtual nodes, so
/// it must be stable across restarts — an id derived from a random port would
/// reshuffle the whole ring every reboot.
pub type NodeId = String;

/// Everything the cluster needs to know to reach a peer: who it is, where its
/// data API lives (HTTP/TCP), and where its gossip endpoint lives (UDP).
///
/// This is what gets gossiped around in V3, so it derives `Serialize`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Node {
    pub id: NodeId,
    /// Where this node serves the client + internal HTTP API.
    pub http_addr: SocketAddr,
    /// Where this node listens for SWIM gossip datagrams (UDP).
    pub gossip_addr: SocketAddr,
}

impl Node {
    pub fn new(id: impl Into<NodeId>, http_addr: SocketAddr, gossip_addr: SocketAddr) -> Self {
        Self {
            id: id.into(),
            http_addr,
            gossip_addr,
        }
    }
}
