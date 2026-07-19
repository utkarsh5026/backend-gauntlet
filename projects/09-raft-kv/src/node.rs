//! The Raft node: the top-level owner that holds all consensus state and drives
//! the timers. Plumbing/wiring — the *decisions* live in the verticals that
//! `impl RaftNode` in their own files: elections (V1, `election.rs`), replication
//! (V2, `replication.rs`), the state machine (V3, `store.rs`), and snapshots
//! (V4, `snapshot.rs`).
//!
//! Concurrency model: all mutable consensus state is behind one `Mutex<Inner>`.
//! One lock (not one per field) keeps the invariants — "term, vote, log, and role
//! move together" — checkable in one place. The lock is `std::sync::Mutex`: hold
//! it to read/mutate state, but **never across an `.await`** (never while sending
//! a peer RPC). Snapshot what you need, drop the guard, then do I/O. Getting that
//! discipline right is part of V1/V2.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

use rand::Rng;
use tracing::{info, warn};

use crate::log::RaftLog;
use crate::peer::PeerClient;
use crate::rpc::{LogIndex, NodeId, Term};
use crate::store::Store;

/// What a node is right now. Every node is exactly one of these at a time; a term
/// has at most one leader.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Follower,
    Candidate,
    Leader,
}

/// Static cluster + timing config, from env in `main`.
#[derive(Debug, Clone)]
pub struct RaftConfig {
    /// How often a leader sends heartbeats. Must be comfortably less than
    /// `election_timeout_min`, or followers time out under a healthy leader.
    pub heartbeat_interval: Duration,
    /// Election timeout is drawn uniformly from `[min, max]` *per attempt*. The
    /// randomness is what desynchronizes followers and breaks split votes (V1).
    pub election_timeout_min: Duration,
    pub election_timeout_max: Duration,
    /// Take a snapshot once this many entries have piled up past the last one (V4).
    pub snapshot_threshold: u64,
}

/// The mutable consensus state, guarded as one unit. Split into Raft's
/// persistent / volatile / leader-only groups.
pub struct Inner {
    // --- persistent (via `log.persist()`): term, vote, and entries ---
    pub log: RaftLog,

    // --- volatile, all nodes ---
    pub role: Role,
    /// Highest log index known to be committed (safe to apply).
    pub commit_index: LogIndex,
    /// Who this node currently believes the leader is (for client redirects).
    pub leader_id: Option<NodeId>,

    // --- volatile, leader only (reinitialized on winning an election) ---
    /// Per-follower: the next index the leader will try to send.
    pub next_index: HashMap<NodeId, LogIndex>,
    /// Per-follower: the highest index known replicated — drives commit advance.
    pub match_index: HashMap<NodeId, LogIndex>,
}

/// One Raft node. Cloned into handlers and the driver task via `Arc`.
pub struct RaftNode {
    pub id: NodeId,
    pub config: RaftConfig,
    /// The other nodes' ids (this node excluded) — the set to canvass and replicate to.
    pub peers: Vec<NodeId>,
    /// This node's own client-facing address, so it can name itself as leader.
    pub self_addr: String,
    /// Peer id → client-facing address, for building redirect responses.
    pub peer_addrs: HashMap<NodeId, String>,

    pub inner: Mutex<Inner>,
    pub store: Store,
    /// Node-to-node RPC transport (fully wired — see `peer.rs`).
    pub peer_client: PeerClient,
}

impl RaftNode {
    /// Build a node. Everything starts as a follower at term 0 with an empty
    /// volatile state — exactly the state a just-booted or just-crashed node is in
    /// before it hears from anyone.
    pub fn new(
        id: NodeId,
        config: RaftConfig,
        self_addr: String,
        peer_addrs: HashMap<NodeId, String>,
        log: RaftLog,
    ) -> Self {
        let peers: Vec<NodeId> = peer_addrs.keys().copied().collect();
        let peer_client = PeerClient::new(peer_addrs.clone());
        Self {
            id,
            config,
            peers,
            self_addr,
            peer_addrs,
            inner: Mutex::new(Inner {
                log,
                role: Role::Follower,
                commit_index: 0,
                leader_id: None,
                next_index: HashMap::new(),
                match_index: HashMap::new(),
            }),
            store: Store::new(),
            peer_client,
        }
    }

    /// Cluster size including this node. A proposal commits once a **majority**
    /// (`cluster_size / 2 + 1`) has it — the quorum that guarantees any two
    /// majorities overlap, so a committed entry can never be lost to a new leader.
    pub fn cluster_size(&self) -> usize {
        self.peers.len() + 1
    }

    /// The number of nodes (including self) that must agree for a majority.
    pub fn quorum(&self) -> usize {
        self.cluster_size() / 2 + 1
    }

    /// A fresh, randomized election timeout. Wired helper (V1 leans on it): the
    /// per-attempt randomness is the anti-split-vote mechanism.
    pub fn random_election_timeout(&self) -> Duration {
        let (lo, hi) = (
            self.config.election_timeout_min,
            self.config.election_timeout_max,
        );
        rand::rng().random_range(lo..=hi)
    }

    /// A cheap status snapshot for `GET /status` and metrics. Wired: reads the
    /// state under the lock and lets go immediately.
    pub fn status(&self) -> serde_json::Value {
        let inner = self.inner.lock().unwrap();
        serde_json::json!({
            "id": self.id,
            "role": inner.role,
            "term": inner.log.current_term(),
            "leader_id": inner.leader_id,
            "commit_index": inner.commit_index,
            "last_applied": self.store.last_applied(),
            "log_last_index": inner.log.last_index(),
            "cluster_size": self.cluster_size(),
        })
    }

    /// Resolve the current leader's client address for a redirect (used by
    /// `AppError::NotLeader`). Wired.
    pub fn leader_hint(&self) -> (Option<NodeId>, Option<String>) {
        let leader = self.inner.lock().unwrap().leader_id;
        let addr = leader.and_then(|id| {
            if id == self.id {
                Some(self.self_addr.clone())
            } else {
                self.peer_addrs.get(&id).cloned()
            }
        });
        (leader, addr)
    }

    /// The driver loop — the clock that makes Raft go. Spawned once from `main`.
    ///
    /// This is the wiring seam for V1 + V2. The scaffold boots the node into an
    /// idle follower and *does not* start consensus, so the process comes up clean
    /// and serves `/status` and `/healthz`; the first client write or inbound RPC
    /// is what hits a `todo!()`. Replace this body with the real loop:
    ///
    /// ```text
    /// TODO(V1/V2): a select! loop over two timers —
    ///   • an election timer, reset on every valid heartbeat / granted vote.
    ///     When it fires as a follower/candidate → become candidate, bump term,
    ///     vote for self, RequestVote all peers (V1: `start_election`).
    ///   • a heartbeat ticker, active only as leader. Each tick → send
    ///     AppendEntries (possibly empty) to every peer (V2: `broadcast_append_entries`).
    /// Plus: whenever `commit_index` advances, apply `commit_index`-`last_applied`
    /// entries to the Store in order (V2 → V3), and trigger a snapshot once the
    /// log passes `snapshot_threshold` (V4).
    /// ```
    pub async fn run(self: std::sync::Arc<Self>) {
        warn!(
            node = self.id,
            "raft driver started in SCAFFOLD mode — consensus not implemented \
             (see TODO(V1/V2) in node.rs::run). Node will idle as a follower."
        );
        // Idle until shutdown. The real loop replaces this with the two-timer
        // select! described above.
        std::future::pending::<()>().await;
    }

    /// Step down to follower at a newer term. A tiny but load-bearing helper: the
    /// "if a message carries a higher term, adopt it and revert to follower" rule
    /// appears in every RPC path, so it's centralized here. Wired mechanics;
    /// callers decide *when* (that's V1/V2), and must `persist()` after.
    pub fn become_follower(inner: &mut Inner, term: Term, leader: Option<NodeId>) {
        if term > inner.log.current_term() {
            inner.log.set_current_term(term);
        }
        inner.role = Role::Follower;
        inner.leader_id = leader;
        info!(term, ?leader, "stepped down to follower");
    }
}

/// A default-ish config sourced by `main` from env. Centralized so the timing
/// relationship (heartbeat ≪ election timeout) is visible in one place.
pub fn config_from_env(
    heartbeat_ms: u64,
    election_min_ms: u64,
    election_max_ms: u64,
    snapshot_threshold: u64,
) -> RaftConfig {
    RaftConfig {
        heartbeat_interval: Duration::from_millis(heartbeat_ms),
        election_timeout_min: Duration::from_millis(election_min_ms),
        election_timeout_max: Duration::from_millis(election_max_ms),
        snapshot_threshold,
    }
}
