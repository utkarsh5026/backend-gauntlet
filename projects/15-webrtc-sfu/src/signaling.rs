//! The signaling plane — **wired**, not a vertical.
//!
//! Before any media flows, the two sides have to agree on *who is talking to whom* and
//! exchange ICE credentials. In real WebRTC that's an **SDP offer/answer** carried over
//! whatever channel you like; here it's a small JSON HTTP API that does the same job without
//! the SDP grammar (parsing full SDP is a documented stretch, not the learning). A publisher
//! `POST`s its simulcast layers and gets back ICE creds + the media address to connect to; a
//! subscriber `POST`s the publisher it wants and gets its own creds + the stable SSRC it will
//! receive on. Those calls build the room/peer graph the [`Sfu`] core forwards over.

use std::sync::Arc;

use axum::extract::{Path, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;

use crate::error::Result;
use crate::sfu::{PeerHandle, PeerId, Sfu};
use crate::simulcast::SimulcastLayer;

/// One announced simulcast encoding in a publish request.
#[derive(Debug, Deserialize)]
struct LayerReq {
    rid: String,
    ssrc: u32,
    bitrate_bps: u32,
}

#[derive(Debug, Deserialize)]
struct PublishReq {
    layers: Vec<LayerReq>,
    /// The client's own ICE ufrag (the remote side of the exchange). Optional for curl smoke tests.
    #[serde(default)]
    client_ufrag: String,
}

#[derive(Debug, Deserialize)]
struct SubscribeReq {
    publisher: PeerId,
    #[serde(default)]
    client_ufrag: String,
}

/// Build the signaling router (mounted alongside the admin router in `main`).
pub fn router(sfu: Arc<Sfu>) -> Router {
    Router::new()
        .route("/rooms", get(list_rooms))
        .route("/rooms/{room}/publish", post(publish))
        .route("/rooms/{room}/subscribe", post(subscribe))
        .with_state(sfu)
}

async fn list_rooms(State(sfu): State<Arc<Sfu>>) -> Json<serde_json::Value> {
    Json(sfu.topology())
}

/// `POST /rooms/:room/publish` — announce simulcast layers, get ICE creds + media address.
async fn publish(
    State(sfu): State<Arc<Sfu>>,
    Path(room): Path<String>,
    Json(req): Json<PublishReq>,
) -> Result<Json<PeerHandle>> {
    let layers = req
        .layers
        .into_iter()
        .map(|l| SimulcastLayer {
            rid: l.rid,
            ssrc: l.ssrc,
            bitrate_bps: l.bitrate_bps,
        })
        .collect();
    let handle = sfu.join_publisher(&room, layers, req.client_ufrag)?;
    Ok(Json(handle))
}

/// `POST /rooms/:room/subscribe` — attach to a publisher, get ICE creds + the stable SSRC.
async fn subscribe(
    State(sfu): State<Arc<Sfu>>,
    Path(room): Path<String>,
    Json(req): Json<SubscribeReq>,
) -> Result<Json<PeerHandle>> {
    let handle = sfu.subscribe(&room, req.publisher, req.client_ufrag)?;
    Ok(Json(handle))
}
