//! The app-facing HTTP surface — **wired**, delegates to the vertical modules.
//!
//! Two audiences share this router:
//!
//! - **Participants** (`/rooms/*`) — the region-aware signaling API. A publisher `POST`s its
//!   simulcast layers; the room is **placed** (V1 picks a home region via consensus if it's new)
//!   and this region is registered active. A subscriber `POST`s the publisher it wants; if that
//!   publisher's media lives in another region, a **cascade relay leg** (V2) is ensured. The ICE
//!   credential exchange + per-subscriber RTP rewrite are **reused from project 15** — the
//!   federation is what's new here.
//! - **Peer SFUs** (`/cluster/*`) — the node-to-node Raft-lite RPCs (V1) that keep the placement
//!   map consistent across the mesh: `POST /cluster/vote` (RequestVote) and `POST /cluster/replicate`
//!   (AppendEntries). Same node-to-node-over-HTTP transport as project 09.
//!
//! The handlers are thin: they parse/authorize and call into [`placement`](crate::placement),
//! [`cascade`](crate::cascade), [`routing`](crate::routing), or [`recording`](crate::recording).
//! Those calls `todo!()` until you implement the vertical — so the routes exist and the router
//! builds, but exercising a path panics with its worklist message. The first `publish` tries to
//! *place* the room (V1) — that panic is the scaffold's to-do list, not a bug.

use axum::extract::{Path, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use tower_http::trace::TraceLayer;

use crate::error::Result;
use crate::placement::PlacementEntry;
use crate::AppState;

/// Build the app router (participant signaling + inter-SFU cluster control). Merged with the admin
/// router in `main` so they share one listener.
pub fn router(state: AppState) -> Router {
    Router::new()
        // --- participant signaling (V1 placement · V2 cascade) ---
        .route("/rooms", get(list_rooms))
        .route("/rooms/{room}/publish", post(publish))
        .route("/rooms/{room}/subscribe", post(subscribe))
        // --- inter-SFU cluster control (V1 consensus RPCs) ---
        .route("/cluster/vote", post(cluster_vote))
        .route("/cluster/replicate", post(cluster_replicate))
        .layer(TraceLayer::new_for_http().make_span_with(common_telemetry::make_request_span))
        .with_state(state)
}

// ---- participant signaling ------------------------------------------------------------------

/// `GET /rooms` — the global topology: each placed room's home region + active regions.
async fn list_rooms(State(state): State<AppState>) -> Json<Value> {
    Json(json!({
        "region": state.node.region,
        "rooms": state.placement.snapshot(),
        "relay_legs": state.cascade.links(),
    }))
}

/// One announced simulcast encoding in a publish request (as in project 15).
#[derive(Debug, Deserialize)]
struct LayerReq {
    rid: String,
    ssrc: u32,
    bitrate_bps: u32,
}

#[derive(Debug, Deserialize)]
struct PublishReq {
    #[serde(default)]
    layers: Vec<LayerReq>,
    /// The client's own ICE ufrag (the remote side of the exchange, consumed by the reused p15
    /// SFU). Optional for curl smoke tests.
    #[serde(default)]
    client_ufrag: String,
}

/// `POST /rooms/{room}/publish` — announce simulcast layers; place the room + register this region.
async fn publish(
    State(state): State<AppState>,
    Path(room): Path<String>,
    Json(req): Json<PublishReq>,
) -> Result<Json<Value>> {
    // Echo the announced ladder (reads every field — the reused p15 SFU consumes these).
    let layers: Vec<Value> = req
        .layers
        .iter()
        .map(|l| json!({ "rid": l.rid, "ssrc": l.ssrc, "bitrate_bps": l.bitrate_bps }))
        .collect();
    let _ = &req.client_ufrag;

    // First publish for a new room PLACES it (V1 consensus): picks the one home region, cluster-wide.
    let placement = state.placement.place_room(&room).await?;
    // This region now has a live participant — replicate membership so the cascade topology agrees.
    state
        .placement
        .register_interest(&room, &state.node.region, true)
        .await?;

    Ok(Json(json!({
        "room": room,
        "home_region": placement.home_region,
        "region": state.node.region,
        "media_addr": state.node.media_addr().to_string(),
        "layers": layers,
    })))
}

#[derive(Debug, Deserialize)]
struct SubscribeReq {
    /// The publisher (peer id, as in project 15) this subscriber wants to watch.
    publisher: u64,
    #[serde(default)]
    client_ufrag: String,
}

/// `POST /rooms/{room}/subscribe` — attach to a publisher; ensure a cascade leg if it's remote.
async fn subscribe(
    State(state): State<AppState>,
    Path(room): Path<String>,
    Json(req): Json<SubscribeReq>,
) -> Result<Json<Value>> {
    let _ = &req.client_ufrag;
    // Register this region's interest (V1) so membership — and thus the cascade tree — is consistent.
    state
        .placement
        .register_interest(&room, &state.node.region, true)
        .await?;

    // If the room's home region is elsewhere, ensure a relay leg from it (V2). A local publisher
    // needs no leg — its media never leaves the region (no hairpin).
    if let Some(placement) = state.placement.room(&room) {
        if placement.home_region != state.node.region {
            let stream = format!("{room}/{}", req.publisher);
            state
                .cascade
                .ensure_link(&placement.home_region, &stream)
                .await?;
        }
    }

    Ok(Json(json!({
        "room": room,
        "publisher": req.publisher,
        "region": state.node.region,
        "media_addr": state.node.media_addr().to_string(),
    })))
}

// ---- inter-SFU cluster control (Raft-lite RPCs) ---------------------------------------------

#[derive(Debug, Deserialize)]
struct VoteReq {
    /// The candidate's id (region/node) requesting the vote.
    from: String,
    term: u64,
}

/// `POST /cluster/vote` — a peer's RequestVote. Delegates to placement consensus (V1).
async fn cluster_vote(
    State(state): State<AppState>,
    Json(req): Json<VoteReq>,
) -> Result<Json<Value>> {
    let granted = state.placement.on_vote(&req.from, req.term).await?;
    Ok(Json(
        json!({ "granted": granted, "term": state.placement.term() }),
    ))
}

#[derive(Debug, Deserialize)]
struct ReplicateReq {
    /// The leader's id.
    from: String,
    term: u64,
    /// New placement/membership entries to append + commit (empty = a heartbeat).
    #[serde(default)]
    entries: Vec<PlacementEntry>,
}

/// `POST /cluster/replicate` — a leader's AppendEntries. Delegates to placement consensus (V1).
async fn cluster_replicate(
    State(state): State<AppState>,
    Json(req): Json<ReplicateReq>,
) -> Result<Json<Value>> {
    let success = state
        .placement
        .on_append(&req.from, req.term, req.entries)
        .await?;
    Ok(Json(
        json!({ "success": success, "term": state.placement.term() }),
    ))
}
