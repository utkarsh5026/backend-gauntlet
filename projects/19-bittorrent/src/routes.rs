//! HTTP control plane: add torrents, inspect progress, health, metrics.
//!
//! This is deliberately thin — the *product* is the BitTorrent engine, and this router
//! is how you drive and observe it. The handlers are wired; what they call into
//! ([`Client::add_torrent`]) `todo!()`-panics inside the metainfo parser (V2) until you
//! build it, exactly like the other scaffolds.

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use serde::Deserialize;
use serde_json::{json, Value};

use tower_http::trace::TraceLayer;

use crate::client::{TorrentSource, TorrentStatus};
use crate::error::AppError;
use crate::types::InfoHash;
use crate::AppState;

/// Build the application router (everything except `/metrics`, which closes over the
/// Prometheus handle instead of `AppState` — see [`metrics_router`]).
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        // `.torrent` upload (raw body) and the torrent listing share the collection URL.
        .route("/torrents", get(list_torrents).post(add_torrent_file))
        .route("/torrents/magnet", post(add_magnet))
        .route("/torrents/{info_hash}", get(get_torrent))
        .layer(TraceLayer::new_for_http().make_span_with(common_telemetry::make_request_span))
        .with_state(state)
}

/// The `/metrics` scrape endpoint, kept separate because it closes over the
/// [`PrometheusHandle`] rather than `AppState`.
pub fn metrics_router(handle: PrometheusHandle) -> Router {
    Router::new().route(
        "/metrics",
        get(move || {
            let handle = handle.clone();
            async move { handle.render() }
        }),
    )
}

async fn healthz() -> &'static str {
    "ok"
}

/// `GET /torrents` — every managed torrent and its progress.
async fn list_torrents(State(state): State<AppState>) -> Json<Vec<TorrentStatus>> {
    Json(state.client.status())
}

/// `POST /torrents` — add a torrent from a raw `.torrent` body. Returns its infohash.
///
/// `202 Accepted`: adding kicks off async work (announce + download), so we acknowledge
/// rather than block until complete.
async fn add_torrent_file(
    State(state): State<AppState>,
    body: Bytes,
) -> Result<(StatusCode, Json<Value>), AppError> {
    let info_hash = state
        .client
        .add_torrent(TorrentSource::Torrent(body.to_vec()))
        .await?;
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({ "info_hash": info_hash.to_hex() })),
    ))
}

#[derive(Deserialize)]
struct MagnetBody {
    uri: String,
}

/// `POST /torrents/magnet` — add a torrent from a `magnet:` URI.
async fn add_magnet(
    State(state): State<AppState>,
    Json(body): Json<MagnetBody>,
) -> Result<(StatusCode, Json<Value>), AppError> {
    let info_hash = state
        .client
        .add_torrent(TorrentSource::Magnet(body.uri))
        .await?;
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({ "info_hash": info_hash.to_hex() })),
    ))
}

/// `GET /torrents/{info_hash}` — one torrent's status (404 if unknown).
async fn get_torrent(
    State(state): State<AppState>,
    Path(info_hash): Path<String>,
) -> Result<Json<TorrentStatus>, AppError> {
    let info_hash = InfoHash::from_hex(&info_hash)
        .ok_or_else(|| AppError::BadRequest("info_hash must be 40 hex characters".into()))?;
    state
        .client
        .get(&info_hash)
        .map(Json)
        .ok_or(AppError::NotFound)
}
