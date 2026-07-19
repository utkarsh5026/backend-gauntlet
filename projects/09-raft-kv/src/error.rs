//! One application error type that turns itself into an HTTP response.
//!
//! The variant that makes this project different is [`AppError::NotLeader`]: in
//! Raft, writes (and linearizable reads) may only be served by the leader. A
//! follower that receives one doesn't guess — it redirects the client to the
//! leader it currently believes in. That's a protocol decision, so it lives here.

use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

use crate::rpc::NodeId;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// This node is not the leader. Carries the leader's client address if known,
    /// so the handler can redirect instead of failing.
    #[error("not the leader")]
    NotLeader {
        leader_id: Option<NodeId>,
        leader_addr: Option<String>,
    },

    /// A read hit a key that isn't in the state machine.
    #[error("key not found")]
    KeyNotFound,

    /// The request named a peer that isn't in this node's cluster config.
    #[error("unknown peer")]
    UnknownPeer,

    /// A peer RPC failed at the transport layer (connection refused, timeout, …).
    /// Expected and normal in a cluster — a node being unreachable is the failure
    /// Raft exists to tolerate, not a bug.
    #[error("peer transport error: {0}")]
    Transport(String),

    /// The request was malformed (bad key, bad node id, …).
    #[error("invalid request: {0}")]
    InvalidRequest(String),

    /// A filesystem operation on the persistent state failed.
    #[error(transparent)]
    Io(#[from] std::io::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl From<reqwest::Error> for AppError {
    fn from(e: reqwest::Error) -> Self {
        AppError::Transport(e.to_string())
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        // A known leader gets a real redirect so a client (or a dumb load
        // balancer) can follow it to the node that can actually serve the write.
        if let AppError::NotLeader {
            leader_addr: Some(addr),
            ..
        } = &self
        {
            let location = format!("http://{addr}");
            return (
                StatusCode::TEMPORARY_REDIRECT,
                [(header::LOCATION, location)],
                Json(json!({ "error": "not the leader", "leader": addr })),
            )
                .into_response();
        }

        let status = match &self {
            // Leader unknown (mid-election): tell the client to back off and retry.
            AppError::NotLeader { .. } => StatusCode::SERVICE_UNAVAILABLE,
            AppError::KeyNotFound | AppError::UnknownPeer => StatusCode::NOT_FOUND,
            AppError::InvalidRequest(_) => StatusCode::BAD_REQUEST,
            AppError::Transport(_) => StatusCode::BAD_GATEWAY,
            AppError::Io(_) | AppError::Other(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };

        if status.is_server_error() {
            tracing::error!(error = %self, "request failed");
        }

        let client_msg = if status.is_server_error() {
            "internal server error".to_string()
        } else {
            self.to_string()
        };

        (status, Json(json!({ "error": client_msg }))).into_response()
    }
}
