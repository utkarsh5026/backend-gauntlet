//! V1 — Stream control plane / session lifecycle. `src/control.rs`.
//!
//! This is the brain of the platform: the thing that knows *"stream `abc123` is live,
//! ingested on node-2, transcoded to a 3-rung ABR ladder, HLS playing at `/live/abc123/…`"*.
//! When an RTMP/WebRTC ingest connects, a session is born and walks a state machine —
//! `Offline → Ingesting → Transcoding → Live → Ended` — and each transition fans work out
//! to the other planes: enqueue transcode jobs (V2), point the edge at the packager (V3),
//! open the chat channel (V4). When ingest drops, it tears all of that down.
//!
//! The learning here is **orchestration under failure**: transitions must be *idempotent*
//! (the same ingest webhook can fire twice), the durable record (Postgres) is the source of
//! truth so a control-plane restart *reconciles* live streams rather than losing them, and a
//! half-finished stream never strands transcode workers or leaks an edge route.
//!
//! Scaffold state: [`Platform::new`] and the read-only [`Platform::snapshot`] are wired so the
//! server boots and `/status` shows an (empty) registry. The state-machine methods are the V1
//! `todo!()` worklist.

use std::collections::HashMap;
use std::sync::RwLock;

use serde::Serialize;
use sqlx::PgPool;

use crate::error::Result;

/// One rung of the adaptive-bitrate ladder a stream is transcoded into. The set of
/// renditions a viewer's player can switch between (the master playlist lists these).
#[derive(Clone, Debug, Serialize)]
pub struct Rendition {
    /// Human label used in the HLS path, e.g. `"720p"`.
    pub name: String,
    pub width: u32,
    pub height: u32,
    pub bitrate_kbps: u32,
}

/// The lifecycle of one live stream. Transitions are the interesting part (V1): only
/// certain edges are legal, and each has side effects on the other planes.
#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StreamState {
    /// Known stream key, nobody broadcasting.
    Offline,
    /// Ingest connected; bytes arriving but no renditions yet.
    Ingesting,
    /// Transcode jobs enqueued; ABR ladder filling in.
    Transcoding,
    /// At least the source rendition is packaged and playable at the edge.
    Live,
    /// Ingest dropped; session draining/archived.
    Ended,
}

/// The durable record for one stream. Persisted in Postgres (source of truth) and
/// cached in the in-memory registry for the hot `/status` + routing reads.
#[derive(Clone, Debug, Serialize)]
pub struct StreamSession {
    /// The stream key the broadcaster authenticates ingest with (also the URL slug).
    pub stream_key: String,
    pub state: StreamState,
    /// Which ingest node currently holds the RTMP/WebRTC connection, if any.
    pub ingest_node: Option<String>,
    /// The ABR ladder this stream is being transcoded into.
    pub ladder: Vec<Rendition>,
    /// Unix-millis the current live session started (for uptime + archival).
    pub started_at_ms: Option<i64>,
}

/// Config for the control plane, read from env in `main`.
pub struct PlatformConfig {
    /// Cap on concurrently live streams this control plane admits (backpressure).
    pub max_streams: usize,
    /// The ABR ladder every stream is transcoded into (parsed from `ABR_LADDER`).
    pub ladder: Vec<Rendition>,
    /// Target HLS segment duration in seconds.
    pub segment_secs: f64,
    /// LL-HLS partial-segment duration in seconds (< `segment_secs`).
    pub part_secs: f64,
}

/// The control plane: durable session store + an in-memory registry for hot reads.
pub struct Platform {
    cfg: PlatformConfig,
    db: PgPool,
    /// Hot cache of live sessions keyed by stream key. Postgres is the source of
    /// truth; this is rebuilt from it on startup (reconcile) and kept in sync on
    /// each transition.
    registry: RwLock<HashMap<String, StreamSession>>,
}

impl Platform {
    /// Build the control plane over a Postgres pool. Wiring only — no I/O yet.
    pub fn new(cfg: PlatformConfig, db: PgPool) -> Self {
        Self {
            cfg,
            db,
            registry: RwLock::new(HashMap::new()),
        }
    }

    pub fn config(&self) -> &PlatformConfig {
        &self.cfg
    }

    /// A read-only view of every session currently in the registry (for `/status`).
    pub fn snapshot(&self) -> Vec<StreamSession> {
        self.registry
            .read()
            .expect("registry lock")
            .values()
            .cloned()
            .collect()
    }

    /// Count of streams currently in the `Live` state (drives a `/status` gauge).
    pub fn live_count(&self) -> usize {
        self.registry
            .read()
            .expect("registry lock")
            .values()
            .filter(|s| s.state == StreamState::Live)
            .count()
    }

    // ---- V1 worklist: the state machine + reconciliation -------------------------

    /// TODO(V1): Rebuild the in-memory registry from Postgres on startup so a
    /// control-plane restart *recovers* in-progress streams (Ingesting/Transcoding/Live)
    /// instead of losing them. Any stream whose ingest lease has expired should be
    /// reconciled to `Ended`. Called once from `main` before serving.
    pub async fn reconcile(&self) -> Result<()> {
        let _ = &self.db;
        todo!("V1: load live sessions from Postgres into the registry, expire stale leases")
    }

    /// TODO(V1): An ingest node reports a broadcaster connected with `stream_key`.
    /// Validate the key exists, enforce `max_streams`, create/return the session, and
    /// move it `Offline → Ingesting`. **Idempotent**: the same webhook firing twice must
    /// not create two sessions or double-enqueue transcode work.
    pub async fn on_ingest_start(
        &self,
        stream_key: &str,
        ingest_node: &str,
    ) -> Result<StreamSession> {
        let _ = (stream_key, ingest_node);
        todo!("V1: admit the stream, persist Ingesting, seed the registry")
    }

    /// TODO(V1): Drive a legal state transition and fan out its side effects
    /// (enqueue transcode on `→ Transcoding`, register the edge route on `→ Live`).
    /// Reject illegal edges (e.g. `Offline → Live`) as [`AppError::Conflict`].
    pub async fn transition(&self, stream_key: &str, to: StreamState) -> Result<()> {
        let _ = (stream_key, to);
        todo!("V1: validate the edge, persist, update the registry, fire side effects")
    }

    /// TODO(V1): Ingest dropped. Move the session to `Ended`, tear down transcode
    /// work + the edge route + the chat channel, and archive/finalize. Must be safe
    /// to call for an already-ended or unknown stream (idempotent teardown).
    pub async fn on_ingest_stop(&self, stream_key: &str) -> Result<()> {
        let _ = stream_key;
        todo!("V1: finalize the session, release resources, mark Ended")
    }
}
