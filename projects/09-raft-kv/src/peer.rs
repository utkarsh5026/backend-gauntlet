//! Node-to-node RPC transport. **Plumbing — fully wired, not a vertical.**
//!
//! Raft is defined in terms of two RPCs a node sends to its peers; how those
//! bytes travel is an implementation detail the algorithm doesn't care about. So
//! this is done for you: a thin `reqwest` client that POSTs each RPC as JSON to
//! the peer's `/raft/*` endpoint and decodes the reply. The learning is the
//! consensus logic that *decides what to send and how to react* — not the HTTP.
//!
//! One thing to notice for when you build V1/V2: these calls **return errors**
//! (a peer may be down, slow, or partitioned). That is normal, not exceptional —
//! tolerating it is the entire reason Raft exists. Your election/replication code
//! must treat a failed `request_vote`/`append_entries` as "no answer from that
//! peer this round" and carry on, never as a fatal error.

use std::collections::HashMap;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde::Serialize;

use crate::error::AppError;
use crate::rpc::{
    AppendEntriesArgs, AppendEntriesReply, InstallSnapshotArgs, InstallSnapshotReply, NodeId,
    RequestVoteArgs, RequestVoteReply,
};

/// Sends RPCs to the other nodes. Holds one pooled `reqwest::Client` (cheap to
/// clone, reuses connections) and the peer id → address map.
pub struct PeerClient {
    http: reqwest::Client,
    peers: HashMap<NodeId, String>,
}

impl PeerClient {
    pub fn new(peers: HashMap<NodeId, String>) -> Self {
        // A short per-RPC timeout matters: a hung peer must not stall an election
        // round. Tune against the heartbeat interval.
        let http = reqwest::Client::builder()
            .timeout(Duration::from_millis(500))
            .build()
            .expect("reqwest client builds");
        Self { http, peers }
    }

    /// Ask `peer` for its vote (V1).
    pub async fn request_vote(
        &self,
        peer: NodeId,
        args: &RequestVoteArgs,
    ) -> Result<RequestVoteReply, AppError> {
        self.post(peer, "/raft/request-vote", args).await
    }

    /// Replicate entries to (or heartbeat) `peer` (V2).
    pub async fn append_entries(
        &self,
        peer: NodeId,
        args: &AppendEntriesArgs,
    ) -> Result<AppendEntriesReply, AppError> {
        self.post(peer, "/raft/append-entries", args).await
    }

    /// Ship a snapshot to a `peer` that has fallen behind the compacted log (V4).
    pub async fn install_snapshot(
        &self,
        peer: NodeId,
        args: &InstallSnapshotArgs,
    ) -> Result<InstallSnapshotReply, AppError> {
        self.post(peer, "/raft/install-snapshot", args).await
    }

    /// POST `body` as JSON to `peer`'s `path` and decode the JSON reply.
    async fn post<Req: Serialize, Res: DeserializeOwned>(
        &self,
        peer: NodeId,
        path: &str,
        body: &Req,
    ) -> Result<Res, AppError> {
        let addr = self.peers.get(&peer).ok_or(AppError::UnknownPeer)?;
        let url = format!("http://{addr}{path}");
        let reply = self
            .http
            .post(url)
            .json(body)
            .send()
            .await?
            .error_for_status()?
            .json::<Res>()
            .await?;
        Ok(reply)
    }
}
