//! The app-facing HTTP/WS surface — **wired**, delegates to the vertical modules.
//!
//! Three planes meet here, each handled by its vertical:
//!
//! - **Ingest control** (`/ingest/*`) — the webhook an RTMP/WebRTC ingest edge calls when a
//!   broadcaster connects/disconnects. Drives the control-plane state machine (V1).
//! - **Playback** (`/live/*`) — HLS master/media playlists + segments, served by the edge (V3).
//! - **Chat** (`/chat/:stream/ws`) — the WebSocket a viewer opens to a channel (V4).
//!
//! The handlers are thin: they parse/authorize and call into [`control`](crate::control),
//! [`edge`](crate::edge), or [`chat`](crate::chat). Those calls `todo!()` until you implement
//! the vertical — so the routes exist and the router builds, but exercising a path panics with
//! its worklist message. That panic is the scaffold's to-do list, not a bug.

use axum::extract::ws::{WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use tower_http::trace::TraceLayer;

use crate::chat::ChatMessage;
use crate::control::StreamState;
use crate::edge::PlaylistCursor;
use crate::error::{AppError, Result};
use crate::AppState;

/// Build the app router (ingest webhook + playback + chat). Merged with the admin
/// router in `main` so they share one listener.
pub fn router(state: AppState) -> Router {
    Router::new()
        // --- ingest control plane (V1) ---
        .route("/ingest/start", post(ingest_start))
        .route("/ingest/stop", post(ingest_stop))
        // --- playback / edge (V3) ---
        .route("/live/{stream}/master.m3u8", get(master_playlist))
        .route("/live/{stream}/{rendition}/index.m3u8", get(media_playlist))
        .route("/live/{stream}/{rendition}/{segment}", get(segment))
        // --- chat (V4) ---
        .route("/chat/{stream}/ws", get(chat_ws))
        .layer(TraceLayer::new_for_http().make_span_with(common_telemetry::make_request_span))
        .with_state(state)
}

// ---- ingest control plane -------------------------------------------------------

#[derive(Debug, Deserialize)]
struct IngestStart {
    /// The stream key the broadcaster authenticated with (secret — never logged).
    stream_key: String,
    /// The ingest node that holds the RTMP/WebRTC connection.
    ingest_node: String,
}

/// `POST /ingest/start` — an ingest edge reports a broadcaster connected.
async fn ingest_start(
    State(state): State<AppState>,
    Json(body): Json<IngestStart>,
) -> Result<Response> {
    let session = state
        .platform
        .on_ingest_start(&body.stream_key, &body.ingest_node)
        .await?;
    Ok(Json(session).into_response())
}

#[derive(Debug, Deserialize)]
struct IngestStop {
    stream_key: String,
}

/// `POST /ingest/stop` — an ingest edge reports a broadcaster disconnected.
async fn ingest_stop(
    State(state): State<AppState>,
    Json(body): Json<IngestStop>,
) -> Result<Response> {
    state.platform.on_ingest_stop(&body.stream_key).await?;
    // Keep the control-plane transition worklist visible to `dead_code`.
    let _ = StreamState::Ended;
    Ok(Json(serde_json::json!({ "ok": true })).into_response())
}

// ---- playback / edge ------------------------------------------------------------

/// `GET /live/{stream}/master.m3u8` — the ABR master playlist.
async fn master_playlist(
    State(state): State<AppState>,
    Path(stream): Path<String>,
) -> Result<Response> {
    let body = state.edge.master_playlist(&stream).await?;
    Ok(m3u8(body))
}

/// LL-HLS blocking-reload query params: `_HLS_msn` / `_HLS_part`.
#[derive(Debug, Deserialize)]
struct ReloadParams {
    #[serde(rename = "_HLS_msn")]
    msn: Option<u64>,
    #[serde(rename = "_HLS_part")]
    part: Option<u32>,
}

/// `GET /live/{stream}/{rendition}/index.m3u8` — a rendition's media playlist (LL-HLS).
async fn media_playlist(
    State(state): State<AppState>,
    Path((stream, rendition)): Path<(String, String)>,
    Query(reload): Query<ReloadParams>,
) -> Result<Response> {
    let cursor = reload.msn.map(|msn| PlaylistCursor {
        msn,
        part: reload.part,
    });
    let body = state
        .edge
        .media_playlist(&stream, &rendition, cursor)
        .await?;
    Ok(m3u8(body))
}

/// `GET /live/{stream}/{rendition}/{segment}` — a segment or partial (byte-range capable).
async fn segment(
    State(state): State<AppState>,
    Path((stream, rendition, segment)): Path<(String, String, String)>,
) -> Result<Response> {
    // TODO(V3 / protocol): parse the `Range` header and pass it through for seeking.
    let bytes = state
        .edge
        .segment(&stream, &rendition, &segment, None)
        .await?;
    Ok((
        [(axum::http::header::CONTENT_TYPE, "video/iso.segment")],
        bytes,
    )
        .into_response())
}

/// Render an m3u8 body with the right content type.
fn m3u8(body: String) -> Response {
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "application/vnd.apple.mpegurl",
        )],
        body,
    )
        .into_response()
}

// ---- chat ----------------------------------------------------------------------

/// `GET /chat/{stream}/ws` — upgrade to a WebSocket subscribed to the channel's chat.
async fn chat_ws(
    State(state): State<AppState>,
    Path(stream): Path<String>,
    ws: WebSocketUpgrade,
) -> Response {
    ws.on_upgrade(move |socket| chat_socket(state, stream, socket))
}

/// One viewer's chat socket. TODO(V4): join the channel, then pump the channel's
/// broadcast receiver → socket (handling `Lagged` per the slow-consumer policy) while
/// reading inbound messages → [`ChatHub::publish`](crate::chat::ChatHub::publish). This is
/// the split-halves fan-out from project 03; the join/publish it calls are the V4 `todo!()`.
async fn chat_socket(state: AppState, stream: String, socket: WebSocket) {
    let _ = (
        socket,
        ChatMessage {
            stream_key: stream.clone(),
            user: String::new(),
            body: String::new(),
            sent_at_ms: 0,
        },
    );
    match state.chat.join(&stream) {
        Ok(_rx) => todo!("V4: pump broadcast receiver <-> socket, publish inbound messages"),
        Err(_) => todo!("V4: reject the socket cleanly when the channel can't be joined"),
    }
}

/// Small helper so the `AppError` import is exercised even before the handlers above
/// are fully fleshed out; returns the not-found error the playback paths will use.
#[allow(dead_code)]
fn not_found(what: &str) -> AppError {
    AppError::NotFound(what.to_string())
}
