//! V1 — Global room placement via consensus. `src/placement.rs`.
//!
//! This is the control plane every SFU in the mesh must agree on. When a room is first created,
//! *some* region is chosen as its **home** — the anchor where the publisher's origin media lives
//! and where the cascade tree (V2/V3) roots — and that decision must be **identical on every
//! node**, or two people creating the same room from opposite sides of the planet get two
//! disjoint conferences under one id (a *split room*). Deciding one value, cluster-wide, under
//! concurrency and partition, is a **consensus** problem — exactly what your Raft work in
//! **project 09** is for.
//!
//! You're **leaning on** project 09's ideas (randomized election timeout → elect a leader,
//! append-entries → replicate, a commit index, idempotent apply), not rebuilding Raft from
//! scratch. What's *yours* here is modelling **room placement + region membership** as the
//! replicated state machine and getting the safety property right: **at most one home region per
//! room, cluster-wide, forever** — and a minority partition that *cannot* invent a new one.
//!
//! Scaffold state: [`Placement::new`] and the read-only [`Placement::snapshot`] / [`role`] /
//! [`leader`] are wired so the server boots and `/status` shows an (empty) map. The consensus
//! methods — placing a room, replicating membership, the vote/append RPC handlers, and the
//! election tick — are the V1 `todo!()` worklist.

use std::collections::{BTreeSet, HashMap};
use std::sync::RwLock;
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::error::Result;

/// A peer SFU in the mesh: its region label, its HTTP control base (for the `/cluster/*` Raft-lite
/// RPCs), and its cascade UDP address (where V2 relays media). Parsed from `PEERS` in `main`.
#[derive(Clone, Debug, Serialize)]
pub struct PeerNode {
    pub region: String,
    /// e.g. `http://10.0.0.2:8080` — where this node POSTs vote/replicate RPCs.
    pub control_addr: String,
    /// e.g. `10.0.0.2:7100` — where this node sends relay media (V2).
    pub media_addr: String,
}

/// This node's role in the Raft-lite placement group. Same three states as project 09.
#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Follower,
    Candidate,
    Leader,
}

impl Role {
    /// Numeric encoding for the `conf_node_role` gauge (0 follower, 1 candidate, 2 leader).
    pub fn as_metric(self) -> f64 {
        match self {
            Role::Follower => 0.0,
            Role::Candidate => 1.0,
            Role::Leader => 2.0,
        }
    }
}

/// The committed placement for one room: its single home region and the set of regions with
/// live participants. This is the replicated state machine's value for a room — every node
/// converges to the same `RoomPlacement` for a given `room_id`.
#[derive(Clone, Debug, Serialize)]
pub struct RoomPlacement {
    pub room_id: String,
    /// The one home region, chosen once by consensus. The invariant V1 protects.
    pub home_region: String,
    /// Regions with ≥1 live participant — the cascade topology (V2/V3) is derived from this.
    pub active_regions: BTreeSet<String>,
    /// Bumped on each committed change to this room's placement (membership included).
    pub epoch: u64,
}

/// One entry in the replicated log: the placement state machine's inputs. Applying committed
/// entries in log order yields the same room map on every node (project 09's apply step).
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum PlacementEntry {
    /// Claim `region` as the home for a not-yet-placed room. Consensus makes exactly one such
    /// claim win per room; later claims are no-ops (idempotent placement).
    PlaceRoom { room_id: String, region: String },
    /// A region gained its first / lost its last participant in a room — replicated membership.
    RegionInterest {
        room_id: String,
        region: String,
        joined: bool,
    },
}

/// Config for the placement group, read from env in `main`.
pub struct PlacementConfig {
    /// This node's region (the home it proposes for rooms created locally).
    pub region: String,
    /// This node's stable id (used in vote records + logs).
    pub node_id: String,
    /// The other SFUs in the mesh (consensus peers).
    pub peers: Vec<PeerNode>,
    /// Randomized election-timeout base; a follower stands after a random interval in
    /// `[election_timeout, 2*election_timeout)` (project 09's split-vote breaker).
    pub election_timeout: Duration,
    /// Leader heartbeat interval (comfortably below `election_timeout`).
    pub heartbeat: Duration,
    /// Cluster-wide cap on placed rooms, enforced through the log (not per-node).
    pub max_rooms: usize,
}

impl PlacementConfig {
    /// Quorum size for the mesh (self + peers). A minority below this cannot place a room.
    pub fn quorum(&self) -> usize {
        (self.peers.len() + 1) / 2 + 1
    }
}

/// Mutable consensus + applied-state, guarded by one lock.
#[derive(Default)]
struct Inner {
    role: Role,
    /// Current Raft-lite term.
    term: u64,
    /// Who this node believes is leader (region/node id), if any.
    leader: Option<String>,
    /// The applied placement state machine: room id → its committed placement. Rebuilt by
    /// applying the replicated log in order; the source of truth for routing reads.
    rooms: HashMap<String, RoomPlacement>,
}

impl Default for Role {
    fn default() -> Self {
        Role::Follower
    }
}

/// The placement control plane: a Raft-lite replicated map of room → home region + membership.
pub struct Placement {
    cfg: PlacementConfig,
    inner: RwLock<Inner>,
}

impl Placement {
    /// Build the placement group. Wiring only — no election runs until [`run`](Self::run).
    pub fn new(cfg: PlacementConfig) -> Self {
        Self {
            cfg,
            inner: RwLock::new(Inner::default()),
        }
    }

    pub fn config(&self) -> &PlacementConfig {
        &self.cfg
    }

    /// This node's current role (for `/status` + the role gauge).
    pub fn role(&self) -> Role {
        self.inner.read().expect("placement lock").role
    }

    /// This node's current term.
    pub fn term(&self) -> u64 {
        self.inner.read().expect("placement lock").term
    }

    /// Who this node believes leads the placement group, if anyone.
    pub fn leader(&self) -> Option<String> {
        self.inner.read().expect("placement lock").leader.clone()
    }

    /// A read-only view of every placed room (for `/status` + `/rooms`). Comes from the
    /// locally-applied map — no cross-region round-trip (the "placement map is the read cache").
    pub fn snapshot(&self) -> Vec<RoomPlacement> {
        self.inner
            .read()
            .expect("placement lock")
            .rooms
            .values()
            .cloned()
            .collect()
    }

    /// The committed placement for one room, if placed (a hot routing read).
    pub fn room(&self, room_id: &str) -> Option<RoomPlacement> {
        self.inner
            .read()
            .expect("placement lock")
            .rooms
            .get(room_id)
            .cloned()
    }

    // ---- V1 worklist: consensus (elect · replicate · apply) ------------------------------

    /// TODO(V1): Ensure `room_id` has a home region, cluster-wide. If it's unplaced, propose a
    /// `PlaceRoom` entry (home = this node's region) and drive it through consensus; if this node
    /// isn't leader, forward to the leader (or return [`AppError::NotLeader`]). **Idempotent**:
    /// an already-placed room returns its existing placement with no second home and no epoch
    /// churn. A minority partition (below [`PlacementConfig::quorum`]) must **refuse**
    /// ([`AppError::Unavailable`]) rather than invent a home — that refusal is what prevents a
    /// split room. Enforces `max_rooms` through the log.
    pub async fn place_room(&self, room_id: &str) -> Result<RoomPlacement> {
        let _ = (
            room_id,
            &self.cfg.region,
            self.cfg.max_rooms,
            self.cfg.quorum(),
        );
        todo!("V1: place the room via consensus (idempotent, one home, minority refuses)")
    }

    /// TODO(V1): Replicate that `region` gained its first (`joined`) or lost its last
    /// (`!joined`) participant in `room_id`, so every node's `active_regions` for that room
    /// converges. Committed through the same log as placement; drives V2/V3 topology.
    pub async fn register_interest(&self, room_id: &str, region: &str, joined: bool) -> Result<()> {
        let _ = (room_id, region, joined);
        todo!("V1: replicate the membership change, apply on commit")
    }

    /// TODO(V1): Handle an inbound RequestVote-style RPC from a peer (`POST /cluster/vote`).
    /// Grant/deny per the term + log-freshness rules from project 09; update term/role.
    pub async fn on_vote(&self, from: &str, term: u64) -> Result<bool> {
        let _ = (from, term);
        todo!("V1: RequestVote — grant at most one vote per term, adopt higher terms")
    }

    /// TODO(V1): Handle an inbound AppendEntries-style RPC from the leader
    /// (`POST /cluster/replicate`): a heartbeat (empty) or new [`PlacementEntry`]s to append +
    /// commit. Reset the election timer, apply newly-committed entries to the room map.
    pub async fn on_append(
        &self,
        from: &str,
        term: u64,
        entries: Vec<PlacementEntry>,
    ) -> Result<bool> {
        let _ = (from, term, entries);
        todo!("V1: AppendEntries — append/commit entries, apply to the map, reset election timer")
    }

    /// TODO(V1): The election + heartbeat loop. As a follower, stand for election on a
    /// randomized timeout (split-vote breaker from p09); as leader, send heartbeats every
    /// [`PlacementConfig::heartbeat`]. Runs until `shutdown` fires. Gated behind `RUN_BACKGROUND`
    /// in `main` so the bare scaffold serves without driving an election with no peers.
    pub async fn run(&self, mut shutdown: tokio::sync::watch::Receiver<bool>) -> Result<()> {
        let _ = (
            &self.cfg.peers,
            self.cfg.election_timeout,
            self.cfg.heartbeat,
            self.cfg.node_id.as_str(),
        );
        let _ = shutdown.changed().await;
        todo!("V1: run the election/heartbeat loop (randomized timeout, single leader per term)")
    }
}
