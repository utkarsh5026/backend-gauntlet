//! HTTP surface: the LL-HLS delivery endpoints a player hits.
//!
//! The routing, path guards, content types, CORS, and blocking-reload param parsing
//! are wired. What the handlers call into — `llhls::media_playlist` and the
//! `LiveStream` byte accessors — is where the V4 `todo!()` (playlist rendering) lives.
//! Run as-is and `GET /healthz` + `GET /live` work; a playlist request against a live
//! stream renders through V4. There is no live stream until a publisher gets past the
//! RTMP handshake (V1), so before then playlist/segment routes are a clean `404`.

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use bytes::Bytes;
use serde_json::json;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::live::LiveRegistry;
use crate::llhls::{self, ReloadParams};

const HLS_PLAYLIST: &str = "application/vnd.apple.mpegurl";
const MP4_INIT: &str = "video/mp4";
const MP4_SEGMENT: &str = "video/iso.segment";

/// Shared state for the HTTP handlers: the live registry (also written by the RTMP
/// sessions).
#[derive(Clone)]
pub struct HttpState {
    pub registry: Arc<LiveRegistry>,
}

/// Build the delivery router.
pub fn router(registry: Arc<LiveRegistry>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/live", get(list_live))
        .route("/live/{key}/index.m3u8", get(media_playlist))
        .route("/live/{key}/init.mp4", get(init_segment))
        .route("/live/{key}/seg/{msn}", get(segment))
        .route("/live/{key}/part/{msn}/{part}", get(part))
        // Browser LL-HLS players (hls.js) fetch cross-origin.
        // TODO(horizontal): tighten from permissive.
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http().make_span_with(common_telemetry::make_request_span))
        .with_state(HttpState { registry })
}

async fn healthz() -> &'static str {
    "ok"
}

/// `GET /live` — the stream keys currently on air.
async fn list_live(State(state): State<HttpState>) -> Json<serde_json::Value> {
    Json(json!({ "live": state.registry.live_keys() }))
}

/// `GET /live/{key}/index.m3u8` — the LL-HLS media playlist, with blocking reload (V4).
async fn media_playlist(
    State(state): State<HttpState>,
    Path(key): Path<String>,
    Query(q): Query<HashMap<String, String>>,
) -> Result<Response, AppError> {
    guard_name(&key)?;
    let stream = state.registry.get(&key).ok_or(AppError::NotFound)?;
    let params = parse_reload_params(&q)?;
    let body = llhls::media_playlist(&stream, params).await?;
    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, HeaderValue::from_static(HLS_PLAYLIST)),
            // The live playlist changes every part — never cache it.
            (header::CACHE_CONTROL, HeaderValue::from_static("no-store")),
        ],
        body,
    )
        .into_response())
}

/// `GET /live/{key}/init.mp4` — the CMAF init segment (immutable, long-cacheable).
async fn init_segment(
    State(state): State<HttpState>,
    Path(key): Path<String>,
) -> Result<Response, AppError> {
    guard_name(&key)?;
    let stream = state.registry.get(&key).ok_or(AppError::NotFound)?;
    let body = stream.init_bytes().ok_or(AppError::NotReady)?;
    Ok(media_bytes(
        body,
        MP4_INIT,
        "public, max-age=31536000, immutable",
    ))
}

/// `GET /live/{key}/seg/{msn}` — one complete media segment (immutable once closed).
async fn segment(
    State(state): State<HttpState>,
    Path((key, msn)): Path<(String, u64)>,
) -> Result<Response, AppError> {
    guard_name(&key)?;
    let stream = state.registry.get(&key).ok_or(AppError::NotFound)?;
    let body = stream.segment_bytes(msn).ok_or(AppError::NotFound)?;
    Ok(media_bytes(
        body,
        MP4_SEGMENT,
        "public, max-age=31536000, immutable",
    ))
}

/// `GET /live/{key}/part/{msn}/{part}` — one partial segment (short-lived).
async fn part(
    State(state): State<HttpState>,
    Path((key, msn, part)): Path<(String, u64, u64)>,
) -> Result<Response, AppError> {
    guard_name(&key)?;
    let stream = state.registry.get(&key).ok_or(AppError::NotFound)?;
    let body = stream.part_bytes(msn, part).ok_or(AppError::NotFound)?;
    Ok(media_bytes(body, MP4_SEGMENT, "public, max-age=5"))
}

/// Build a `200` response for an immutable/short-lived media body.
fn media_bytes(body: Bytes, content_type: &'static str, cache_control: &'static str) -> Response {
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, HeaderValue::from_static(content_type)),
            (
                header::CACHE_CONTROL,
                HeaderValue::from_static(cache_control),
            ),
        ],
        body,
    )
        .into_response()
}

/// Parse the LL-HLS blocking-reload query params (`_HLS_msn` / `_HLS_part` / `_HLS_skip`).
fn parse_reload_params(q: &HashMap<String, String>) -> Result<ReloadParams, AppError> {
    let parse = |k: &str| -> Result<Option<u64>, AppError> {
        q.get(k)
            .map(|v| {
                v.parse::<u64>()
                    .map_err(|_| AppError::BadRequest(format!("bad {k}")))
            })
            .transpose()
    };
    Ok(ReloadParams {
        msn: parse("_HLS_msn")?,
        part: parse("_HLS_part")?,
        skip: q.get("_HLS_skip").map(|v| v == "YES").unwrap_or(false),
    })
}

/// Reject any stream key that could escape the in-memory store or a work dir. Axum
/// won't route a `/` inside a single `{param}`, but an empty name, `.`/`..`, a NUL, or
/// a backslash still shouldn't reach a lookup.
fn guard_name(name: &str) -> Result<(), AppError> {
    let safe = !name.is_empty() && name != "." && name != ".." && !name.contains(['/', '\\', '\0']);
    if safe {
        Ok(())
    } else {
        Err(AppError::BadRequest(format!(
            "invalid path segment: {name:?}"
        )))
    }
}
