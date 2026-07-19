//! V4 — Replication & request coordination.
//!
//! Sharding alone (V2 + V3) means losing a node loses its whole shard. This layer
//! makes the cache *survivable* and *location-transparent*:
//!
//!   - **Replication:** a key lives on the first `replication_factor` nodes
//!     clockwise on the ring, so one node's death doesn't lose the value.
//!   - **Coordination:** any node can take any request. It asks the ring who owns
//!     the key; if it's one of the owners it serves locally, otherwise it
//!     forwards to an owner and proxies the answer back. The client never learns
//!     the topology.
//!
//! The consistency you offer is a *choice you make and document* — this is a
//! cache, so a common pick is W=1 + async replication (fast, may briefly stale)
//! rather than a database's R+W>N quorum. Name it in `docs/07-design.md`.
//!
//! Scaffold state: the coordinator is wired with the local store + membership,
//! and the *local* operations (`local_*`, used when a forwarded request lands on
//! an owner) just call the store. The routing/forwarding brain is `todo!()`.

use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;

use crate::error::AppError;
use crate::membership::Membership;
use crate::node::{Node, NodeId};
use crate::store::Store;

/// Routes cache operations to the nodes that own each key, replicating writes and
/// proxying reads. Cloned cheaply (everything behind `Arc`).
#[derive(Clone)]
pub struct Coordinator {
    self_id: NodeId,
    store: Arc<Store>,
    membership: Arc<Membership>,
    /// How many ring successors hold a copy of each key (N in the SPEC).
    replication_factor: usize,
}

impl Coordinator {
    pub fn new(
        self_id: NodeId,
        store: Arc<Store>,
        membership: Arc<Membership>,
        replication_factor: usize,
    ) -> Arc<Self> {
        assert!(replication_factor >= 1, "replication factor must be >= 1");
        Arc::new(Self {
            self_id,
            store,
            membership,
            replication_factor,
        })
    }

    /// The alive replica nodes for `key`, in ring order. Helper the routing
    /// methods below will lean on once V2/V3 make `replicas`/`resolve` real.
    fn replica_nodes(&self, key: &str) -> Vec<Node> {
        self.membership
            .replicas(key, self.replication_factor)
            .into_iter()
            .filter(|id| self.membership.is_alive(id))
            .filter_map(|id| self.membership.resolve(&id))
            .collect()
    }

    /// GET a key from wherever it lives. If this node is a replica, serve locally;
    /// otherwise forward to one and proxy the bytes back.
    pub async fn get(&self, key: &str) -> Result<Option<Bytes>, AppError> {
        // TODO(V4): resolve replica_nodes(key). If empty → Err(Unavailable). If
        // one is *this* node → self.local_get(key). Otherwise forward the GET to
        // a replica's http_addr (GET /internal/cache/{key}) and return its answer;
        // on a replica error, try the next replica before giving up (read
        // failover). This is where you decide your read policy (R=1 vs read-repair).
        let _ = (&self.self_id, &self.store, &self.membership, key);
        todo!("V4: route a GET to a replica (local if we own it, else forward)")
    }

    /// PUT a key onto all of its replicas per the write policy.
    pub async fn put(
        &self,
        key: String,
        value: Bytes,
        ttl: Option<Duration>,
    ) -> Result<(), AppError> {
        // TODO(V4): resolve replica_nodes(&key). Write to each: local ones via
        // self.local_put, remote ones via PUT /internal/cache/{key}. Your write
        // policy decides when to ack — W=1 (ack after the first, replicate the
        // rest in the background) or W=majority. Document which and why (it's a
        // cache: availability usually wins).
        let _ = (
            &self.self_id,
            &self.store,
            &self.membership,
            self.replication_factor,
            key,
            value,
            ttl,
        );
        todo!("V4: replicate a PUT to the key's replica set per the write policy")
    }

    /// DELETE a key from all of its replicas.
    pub async fn delete(&self, key: &str) -> Result<(), AppError> {
        // TODO(V4): same fan-out as put — remove on every replica (local + remote
        // via DELETE /internal/cache/{key}).
        let _ = (&self.store, &self.membership, key);
        todo!("V4: delete a key from all of its replicas")
    }

    // --- local operations -----------------------------------------------------
    // These run when *this* node is an owner (a directly-addressed request, or one
    // forwarded to us by a peer coordinator). They only touch the local store —
    // no routing — so they're wired now; the internal HTTP routes call straight
    // into them.

    pub fn local_get(&self, key: &str) -> Option<Bytes> {
        self.store.get(key)
    }

    pub fn local_put(&self, key: String, value: Bytes, ttl: Option<Duration>) {
        self.store.put(key, value, ttl)
    }

    pub fn local_delete(&self, key: &str) -> bool {
        self.store.remove(key)
    }
}
