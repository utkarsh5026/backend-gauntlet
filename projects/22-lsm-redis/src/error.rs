//! One application error type, shared by the two front-ends.
//!
//! The engine and its layers (WAL, memtable, SSTable, block cache) all return
//! `Result<_, AppError>`. Two things consume it:
//!   - the **RESP** path ([`crate::server`]) turns an error into a `-ERR …` reply
//!     via [`AppError::to_resp_error`] — the client sees a redis-shaped error line;
//!   - the **HTTP sidecar** ([`crate::routes`]) turns it into a JSON status code via
//!     [`IntoResponse`].
//!
//! The variants track the engine's vocabulary — a wrong-type operation, a corrupt
//! frame found on read, an auth failure — so both mappings stay direct and no
//! internal detail (paths, offsets) leaks to a client.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// The client issued a command against a key holding the wrong data type
    /// (redis `WRONGTYPE`). Reserved for when you grow past raw string GET/SET.
    #[error("WRONGTYPE Operation against a key holding the wrong kind of value")]
    WrongType,

    /// Missing / wrong password on a connection when `REQUIREPASS` is set
    /// (redis `NOAUTH` / `WRONGPASS`).
    #[error("NOAUTH Authentication required")]
    Unauthorized,

    /// The wire bytes were not a valid RESP frame, or a command had the wrong
    /// arity / argument shape. Maps to `-ERR …` on RESP, `400` on HTTP.
    #[error("ERR protocol error: {0}")]
    Protocol(String),

    /// A WAL frame or SSTable block failed its CRC / format check (V2/V4) —
    /// corruption or a torn tail a clean write should never have produced. Never
    /// returned to a client as data; surfaces as a server error.
    #[error("corrupt data on disk")]
    Corrupt,

    /// A filesystem operation failed (the data directory *is* the database).
    #[error(transparent)]
    Io(#[from] std::io::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl AppError {
    /// Render this error as a RESP simple-error payload (the text after the `-`,
    /// without the trailing CRLF) — e.g. `ERR protocol error: …`. On a server-side
    /// fault we hide the detail behind a generic line so paths/offsets never leak.
    pub fn to_resp_error(&self) -> String {
        match self {
            AppError::Corrupt | AppError::Io(_) | AppError::Other(_) => {
                tracing::error!(error = %self, "command failed");
                "ERR internal error".to_string()
            }
            other => other.to_string(),
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::Unauthorized => StatusCode::UNAUTHORIZED,
            AppError::WrongType | AppError::Protocol(_) => StatusCode::BAD_REQUEST,
            AppError::Corrupt | AppError::Io(_) | AppError::Other(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
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
