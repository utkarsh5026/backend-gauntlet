//! One application error type that renders itself as an HTTP response for the
//! control plane. The engine's internals reuse it too so a `?` bubbles up cleanly.
//!
//! A BitTorrent client's failure modes aren't plain CRUD: a `.torrent` can be
//! malformed, a tracker can time out, a peer can lie or send garbage. Client mistakes
//! (a bad magnet, an unknown infohash) are clean 4xx; everything the network throws at
//! us is logged server-side and returned as a generic 5xx so we never leak internals.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No torrent with that infohash is being managed.
    #[error("not found")]
    NotFound,

    /// The request was malformed (bad magnet URI, non-hex infohash, …).
    #[error("bad request: {0}")]
    BadRequest(String),

    /// A `.torrent`/magnet failed to parse or is internally inconsistent (V2).
    #[error("invalid torrent: {0}")]
    InvalidTorrent(String),

    /// A tracker announce failed (bad response, timeout, protocol error) (V3).
    #[error("tracker error: {0}")]
    Tracker(String),

    /// A peer violated the wire protocol — bad handshake, oversized/garbled message
    /// (V4). Never trust a peer; a protocol error closes that connection, not the app.
    #[error("peer protocol error: {0}")]
    Peer(String),

    /// A local I/O error (piece store, socket).
    #[error(transparent)]
    Io(#[from] std::io::Error),

    /// An HTTP tracker request failed at the transport layer (V3).
    #[error(transparent)]
    Http(#[from] reqwest::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::NotFound => StatusCode::NOT_FOUND,
            AppError::BadRequest(_) | AppError::InvalidTorrent(_) => StatusCode::BAD_REQUEST,
            AppError::Tracker(_)
            | AppError::Peer(_)
            | AppError::Io(_)
            | AppError::Http(_)
            | AppError::Other(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };

        // Log the full error server-side; only leak a generic message on 5xx.
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
