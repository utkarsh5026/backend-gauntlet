//! HTTP surface: the HLS/DASH endpoints a player hits.
//!
//! The routing, path validation, content types, and CORS are wired. What the
//! handlers call into — `catalog.*`, which composes `isobmff`/`segment`/`manifest`
//! — is where the `todo!()`s live. Run as-is and `GET /healthz` + `GET /assets`
//! work; the first playlist/segment request panics on a Vx todo, which is the worklist.

use axum::extract::{Path, State};
use axum::http::{header, HeaderMap};
use axum::response::Response;
use axum::routing::get;
use axum::{Json, Router};
use serde_json::json;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;

use crate::delivery;
use crate::error::AppError;
use crate::AppState;

const HLS_PLAYLIST: &str = "application/vnd.apple.mpegurl";
const DASH_MPD: &str = "application/dash+xml";
const INIT_SEGMENT: &str = "video/mp4";
const MEDIA_SEGMENT: &str = "video/iso.segment";

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/assets", get(list_assets))
        // HLS: master playlist (the ABR ladder) + per-rendition media playlist.
        .route("/vod/{asset}/master.m3u8", get(master_playlist))
        .route("/vod/{asset}/{rendition}/index.m3u8", get(media_playlist))
        // DASH manifest for the same segments.
        .route("/vod/{asset}/{rendition}/manifest.mpd", get(dash_manifest))
        // CMAF init segment + media segments (the latter served with Range).
        .route("/vod/{asset}/{rendition}/init.mp4", get(init_segment))
        .route("/vod/{asset}/{rendition}/seg/{index}", get(media_segment))
        // Browser players (hls.js/dash.js) fetch cross-origin; byte-range reads need
        // the range/response headers exposed too.
        // TODO(horizontal): tighten this from permissive and add
        //   Access-Control-Expose-Headers: Content-Range, Content-Length, Accept-Ranges
        // so cross-origin range reads see the headers a player needs.
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http().make_span_with(common_telemetry::make_request_span))
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok"
}

/// `GET /assets` — list the loaded library (asset → rendition ids).
async fn list_assets(State(state): State<AppState>) -> Json<serde_json::Value> {
    let assets: Vec<_> = state
        .catalog
        .asset_names()
        .into_iter()
        .map(|name| json!({ "asset": name, "renditions": state.catalog.rendition_ids(name) }))
        .collect();
    Json(json!({ "assets": assets }))
}

/// `GET /vod/{asset}/master.m3u8` — HLS master playlist (V3/V4).
async fn master_playlist(
    State(state): State<AppState>,
    Path(asset): Path<String>,
) -> Result<Response, AppError> {
    guard_name(&asset)?;
    let body = state.catalog.master_playlist(&asset)?;
    Ok(delivery::text_response(body, HLS_PLAYLIST))
}

/// `GET /vod/{asset}/{rendition}/index.m3u8` — HLS media playlist (V3).
async fn media_playlist(
    State(state): State<AppState>,
    Path((asset, rendition)): Path<(String, String)>,
) -> Result<Response, AppError> {
    guard_name(&asset)?;
    guard_name(&rendition)?;
    let body = state.catalog.media_playlist(&asset, &rendition).await?;
    Ok(delivery::text_response(body, HLS_PLAYLIST))
}

/// `GET /vod/{asset}/{rendition}/manifest.mpd` — DASH MPD (V3).
async fn dash_manifest(
    State(state): State<AppState>,
    Path((asset, rendition)): Path<(String, String)>,
) -> Result<Response, AppError> {
    guard_name(&asset)?;
    guard_name(&rendition)?;
    let body = state.catalog.dash_mpd(&asset, &rendition).await?;
    Ok(delivery::text_response(body, DASH_MPD))
}

/// `GET /vod/{asset}/{rendition}/init.mp4` — CMAF init segment (V2).
async fn init_segment(
    State(state): State<AppState>,
    Path((asset, rendition)): Path<(String, String)>,
    headers: HeaderMap,
) -> Result<Response, AppError> {
    guard_name(&asset)?;
    guard_name(&rendition)?;
    let body = state.catalog.init_segment(&asset, &rendition).await?;
    Ok(delivery::serve_ranged(
        body,
        INIT_SEGMENT,
        range_header(&headers),
    ))
}

/// `GET /vod/{asset}/{rendition}/seg/{index}` — one media segment (V2), served with
/// HTTP `Range` (V4).
async fn media_segment(
    State(state): State<AppState>,
    Path((asset, rendition, index)): Path<(String, String, usize)>,
    headers: HeaderMap,
) -> Result<Response, AppError> {
    guard_name(&asset)?;
    guard_name(&rendition)?;
    let body = state
        .catalog
        .media_segment(&asset, &rendition, index)
        .await?;
    Ok(delivery::serve_ranged(
        body,
        MEDIA_SEGMENT,
        range_header(&headers),
    ))
}

/// Pull the `Range` header as a `&str` for [`delivery::serve_ranged`].
fn range_header(headers: &HeaderMap) -> Option<&str> {
    headers.get(header::RANGE).and_then(|v| v.to_str().ok())
}

/// Reject any asset/rendition path segment that could escape `MEDIA_DIR` (security
/// horizontal). Axum won't route a `/` inside a single `{param}`, but an empty
/// name, `.`/`..`, a NUL, or a backslash still shouldn't reach the filesystem.
fn guard_name(name: &str) -> Result<(), AppError> {
    let safe = !name.is_empty() && name != "." && name != ".." && !name.contains(['/', '\\', '\0']);
    if safe {
        Ok(())
    } else {
        Err(AppError::InvalidRequest(format!(
            "invalid path segment: {name:?}"
        )))
    }
}
