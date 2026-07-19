//! V2 — The consistent-hash ring with virtual nodes.
//!
//! This is what decides *which node owns a key* without a coordinator, and — the
//! whole reason it exists — keeps almost every key in place when the node set
//! changes. `hash(key) % N` fails that second property catastrophically: bump `N`
//! and nearly every key remaps, cold-missing the entire cache on a deploy.
//!
//! The ring fixes it: hash both nodes and keys onto the same circular space, and
//! a key belongs to the first node you meet walking clockwise. Adding a node only
//! steals the arc of keys between it and its predecessor (~1/N of them); the rest
//! don't move. **Virtual nodes** — hashing each physical node to many ring
//! positions — keep the arcs evenly sized so one node doesn't randomly own half
//! the keyspace.
//!
//! Pure data structure: no async, no locks (the caller — membership, V3 — wraps
//! it in a lock). That makes it directly unit-testable, which is exactly what the
//! V2 proofs need. Scaffold state: `new` works; the ring operations are `todo!()`.

use crate::node::NodeId;

/// The consistent-hash ring. Owns the mapping from ring position → physical node,
/// spread out via `vnodes_per_node` virtual positions per node.
pub struct Ring {
    /// How many ring positions each physical node occupies. Higher = smoother
    /// load, more memory. A documented choice in the SPEC (typical: 100–200).
    vnodes_per_node: u32,
    // TODO(V2): the ring itself. You want a structure that, given a key's hash,
    // finds the next position >= it in O(log n) — a sorted map keyed by the
    // 64-bit position, whose value is the owning NodeId, is the classic choice:
    //
    //   ring: BTreeMap<u64, NodeId>,
    //
    // (BTreeMap::range(hash..).next(), wrapping to the first entry, is the
    // clockwise walk.) Pick your own; just keep lookup sub-linear per the SPEC.
}

impl Ring {
    /// A ring with no nodes yet. Membership (V3) calls `add_node` as peers join.
    pub fn new(vnodes_per_node: u32) -> Self {
        assert!(vnodes_per_node > 0, "need at least one vnode per node");
        Self { vnodes_per_node }
    }

    pub fn vnodes_per_node(&self) -> u32 {
        self.vnodes_per_node
    }

    /// Place a physical node onto the ring at `vnodes_per_node` positions.
    pub fn add_node(&mut self, node: &NodeId) {
        // TODO(V2): for i in 0..vnodes_per_node, insert ring[hash(node, i)] = node.
        // The `i` is what turns one physical node into many ring positions — hash
        // something like format!("{node}#{i}"). Idempotent: adding a node already
        // present shouldn't duplicate or corrupt its positions.
        let _ = (self.vnodes_per_node, node);
        todo!("V2: insert this node's virtual nodes onto the ring")
    }

    /// Remove a physical node and all of its virtual positions.
    pub fn remove_node(&mut self, node: &NodeId) {
        // TODO(V2): drop every ring position owned by `node`. Only the keys in
        // those arcs move (to the next node clockwise); everything else stays.
        let _ = node;
        todo!("V2: remove all of this node's virtual nodes from the ring")
    }

    /// The physical node that owns `key` — the first one clockwise from the key's
    /// hash. `None` only when the ring is empty.
    pub fn owner(&self, key: &str) -> Option<NodeId> {
        self.replicas(key, 1).into_iter().next()
    }

    /// The first `n` **distinct physical** nodes clockwise from `key`'s hash — the
    /// key's replica set (V4 stores the value on all of them).
    pub fn replicas(&self, key: &str, n: usize) -> Vec<NodeId> {
        // TODO(V2): hash the key, walk the ring clockwise from that position
        // (wrapping past the end back to the start), and collect node ids —
        // SKIPPING vnodes that map to a physical node already chosen, because
        // several vnodes of the same node will sit next to each other. Stop at
        // `n` distinct nodes or when you've seen every node (n > cluster size).
        let _ = (key, n);
        todo!("V2: walk the ring clockwise collecting n distinct physical nodes")
    }

    /// How many physical nodes are currently on the ring (for the balance test /
    /// replica-count clamping).
    pub fn node_count(&self) -> usize {
        todo!("V2: number of distinct physical nodes on the ring")
    }
}

/// Hash arbitrary bytes to a position on the 64-bit ring.
///
/// SHA-256 (already a workspace dep) gives a well-distributed digest; we take its
/// leading 8 bytes as the position. A crypto hash is overkill for load balancing
/// but avoids the "std `DefaultHasher` isn't stable across builds" trap — the ring
/// must hash a node to the *same* position on every machine and every restart.
#[allow(dead_code)] // used once V2's ring operations are implemented
fn ring_position(bytes: &[u8]) -> u64 {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(bytes);
    u64::from_be_bytes(digest[..8].try_into().expect("sha256 yields 32 bytes"))
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove the ring:
    //   - determinism: same key + same membership -> same owner, every time;
    //   - minimal movement: with K random keys spread over N nodes, adding an
    //     (N+1)th node moves ~K/(N+1) keys — assert it's near 1/(N+1), NOT ~1;
    //   - balance: raising vnodes_per_node lowers the spread of keys-per-node
    //     (max/min or stddev shrinks);
    //   - replicas(key, n) returns n DISTINCT physical nodes (never a repeat),
    //     even though vnodes of one node cluster together.
}
