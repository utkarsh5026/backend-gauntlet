//! HTTP surface — two audiences on one server.
//!
//! **Clients** talk to the KV API (`/kv/*`, `/status`). Writes and linearizable
//! reads only succeed on the leader; a follower answers with a redirect to the
//! leader it knows (`AppError::NotLeader`).
//!
//! **Peers** talk to the `/raft/*` RPC endpoints — the receive side of the two
//! consensus RPCs (and `InstallSnapshot`). These deserialize the args, hand them
//! to the node's `handle_*` methods (V1/V2/V4), and serialize the reply. The
//! routing and shapes are wired; what the handlers call into is the `todo!()`.
//!
//! Scaffold behavior: `GET /healthz` and `GET /status` work immediately. A client
//! write, a linearizable read, or any inbound RPC hits a `todo!()` and panics —
//! that panic is the worklist.

use std::sync::Arc;

use axum::extract::{Path, State};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::Deserialize;
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::node::RaftNode;
use crate::rpc::{AppendEntriesArgs, Command, InstallSnapshotArgs, RequestVoteArgs};

/// Shared state: just the node, behind an `Arc`.
#[derive(Clone)]
pub struct AppState {
    pub node: Arc<RaftNode>,
}

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/status", get(status))
        // Client KV API.
        .route("/kv/{key}", put(put_key).get(get_key).delete(delete_key))
        // Peer RPC (V1/V2/V4) — the receive side of the consensus protocol.
        .route("/raft/request-vote", post(request_vote))
        .route("/raft/append-entries", post(append_entries))
        .route("/raft/install-snapshot", post(install_snapshot))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok"
}

/// `GET /status` — role, term, leader, commit/apply progress. Wired; safe to call
/// in any state (it's how you watch an election happen).
async fn status(State(state): State<AppState>) -> Json<serde_json::Value> {
    Json(state.node.status())
}

// ---- Client KV API ---------------------------------------------------------

#[derive(Debug, Deserialize)]
struct PutBody {
    value: String,
}

/// `PUT /kv/{key}` `{ "value": ... }` — a write. Goes through Raft: appended on
/// the leader, replicated to a quorum, applied, then answered (V2 → V3).
async fn put_key(
    State(state): State<AppState>,
    Path(key): Path<String>,
    Json(body): Json<PutBody>,
) -> Result<Json<serde_json::Value>, AppError> {
    if key.is_empty() {
        return Err(AppError::InvalidRequest("empty key".into()));
    }
    let resp = state
        .node
        .propose(Command::Set {
            key: key.clone(),
            value: body.value,
        })
        .await?;
    Ok(Json(
        serde_json::json!({ "key": key, "previous": resp.value }),
    ))
}

/// `GET /kv/{key}` — a linearizable read. Served only by a leader that has
/// confirmed it still leads (V2/V3 read path); `404` if the key is unset.
async fn get_key(
    State(state): State<AppState>,
    Path(key): Path<String>,
) -> Result<Json<serde_json::Value>, AppError> {
    match state.node.read(&key).await? {
        Some(value) => Ok(Json(serde_json::json!({ "key": key, "value": value }))),
        None => Err(AppError::KeyNotFound),
    }
}

/// `DELETE /kv/{key}` — a delete, also a replicated command.
async fn delete_key(
    State(state): State<AppState>,
    Path(key): Path<String>,
) -> Result<Json<serde_json::Value>, AppError> {
    let resp = state
        .node
        .propose(Command::Delete { key: key.clone() })
        .await?;
    Ok(Json(
        serde_json::json!({ "key": key, "previous": resp.value }),
    ))
}

// ---- Peer RPC (V1 / V2 / V4) -----------------------------------------------

/// `POST /raft/request-vote` — a candidate is asking for our vote (V1).
async fn request_vote(
    State(state): State<AppState>,
    Json(args): Json<RequestVoteArgs>,
) -> Json<crate::rpc::RequestVoteReply> {
    Json(state.node.handle_request_vote(args).await)
}

/// `POST /raft/append-entries` — the leader is replicating (or heartbeating) (V2).
async fn append_entries(
    State(state): State<AppState>,
    Json(args): Json<AppendEntriesArgs>,
) -> Json<crate::rpc::AppendEntriesReply> {
    Json(state.node.handle_append_entries(args).await)
}

/// `POST /raft/install-snapshot` — the leader is shipping us a snapshot (V4).
async fn install_snapshot(
    State(state): State<AppState>,
    Json(args): Json<InstallSnapshotArgs>,
) -> Json<crate::rpc::InstallSnapshotReply> {
    Json(state.node.handle_install_snapshot(args).await)
}
