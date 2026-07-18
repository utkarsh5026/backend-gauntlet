//! HTTP surface + the per-connection WebSocket loop.
//!
//! The wiring here is complete: the router, the `GET /ws` upgrade, splitting the
//! socket into a reader and a writer task, parsing client frames, and tearing the
//! connection down on every exit path. What it *calls into* — the hub, the
//! mailbox's overflow policy, presence, the cluster bridge — is where the SPEC's
//! `todo!()`s live. Run this as-is and a publish will panic with "V1: fan-out…",
//! which is exactly the worklist.

use std::time::{Duration, Instant};

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::Response;
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use futures_util::{SinkExt, StreamExt};
use metrics_exporter_prometheus::PrometheusHandle;
use serde::{Deserialize, Serialize};
use tokio::time::timeout;
use tower_http::trace::TraceLayer;
use tracing::{debug, info, warn};
use uuid::Uuid;

use crate::backpressure::{self, Mailbox};
use crate::directory::{Directory, Group, Membership, Person};
use crate::error::AppError;
use crate::protocol::{ClientMessage, ConnId, ServerMessage};
use crate::AppState;

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/debug/health", get(deps_health))
        .route("/ws", get(ws_handler))
        .nest(
            "/admin",
            Router::new()
                .route("/people", get(list_people).post(create_person))
                .route("/people/{id}", delete(delete_person))
                .route("/groups", get(list_groups).post(create_group))
                .route(
                    "/people/{person_id}/groups/{group_id}",
                    post(add_member).delete(remove_member),
                )
                .route("/memberships", get(list_memberships)),
        )
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

// --- Admin directory handlers ---------------------------------------------------
//
// The HTTP wiring here is complete; the SQL it calls into lives in directory.rs
// (`todo!()` until you write it). Hitting an endpoint before then panics with the
// module's todo message — that's the worklist, same as the verticals.

/// Pull the directory out of state, or 503 if the roster DB is disabled (no
/// `DATABASE_URL`). The pub/sub core runs without it; only `/admin` needs it.
fn directory(state: &AppState) -> Result<&Directory, AppError> {
    state
        .directory
        .as_ref()
        .ok_or_else(|| AppError::Unavailable("directory disabled: set DATABASE_URL".into()))
}

// --- Dependency health (devtools) ----------------------------------------------
//
// `GET /debug/health` live-probes the OPTIONAL backing stores so the web devtools
// panel can show an up/down light for each. This is NOT the liveness probe
// (`/healthz` stays a bare "ok" for scrapers) and NOT part of the store-free
// pub/sub core — it's playground observability, same tier as the `/admin` roster.

/// How long one dependency probe may run before we call it down — stops a dead
/// store from hanging the endpoint (and the devtools poll behind it).
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

/// Status of one backing store. `state` is `"up" | "down" | "disabled"`.
#[derive(Debug, Serialize)]
struct DepStatus {
    state: &'static str,
    /// The error on `down`, the reason on `disabled`; omitted when `up`.
    #[serde(skip_serializing_if = "Option::is_none")]
    detail: Option<String>,
    /// Probe round-trip in milliseconds — present only when `up`.
    #[serde(skip_serializing_if = "Option::is_none")]
    latency_ms: Option<f64>,
}

impl DepStatus {
    fn up(elapsed: Duration) -> Self {
        Self {
            state: "up",
            detail: None,
            latency_ms: Some(elapsed.as_secs_f64() * 1000.0),
        }
    }
    fn down(detail: impl Into<String>) -> Self {
        Self {
            state: "down",
            detail: Some(detail.into()),
            latency_ms: None,
        }
    }
    fn disabled(reason: &'static str) -> Self {
        Self {
            state: "disabled",
            detail: Some(reason.to_string()),
            latency_ms: None,
        }
    }
}

/// A snapshot of every backing store the app can talk to.
#[derive(Debug, Serialize)]
struct DepsHealth {
    /// Postgres (admin roster). `disabled` when `DATABASE_URL` is unset.
    db: DepStatus,
    /// Redis (cross-node bus). Probed for reachability even in single-node mode.
    redis: DepStatus,
    /// Whether the app is actually bridging through Redis (V4 / `CLUSTER=true`).
    /// `false` means Redis may be up but the pub/sub core isn't using it.
    cluster_mode: bool,
    /// Whether `WS_AUTH_TOKEN` is set. When `false`, EVERY `/ws` upgrade is
    /// rejected with 401 (fail closed) and nobody can come online. Only the
    /// boolean is reported — never the secret itself.
    ws_auth_configured: bool,
}

async fn deps_health(State(state): State<AppState>) -> Json<DepsHealth> {
    // Postgres: `SELECT 1` against the roster pool. Runtime (unchecked) SQL on
    // purpose — a liveness ping needs no compile-time schema check and shouldn't
    // land in the sqlx offline cache.
    let db = match state.directory.as_ref() {
        None => DepStatus::disabled("DATABASE_URL unset"),
        Some(dir) => {
            let start = Instant::now();
            let query = sqlx::query("SELECT 1").execute(dir.pool());
            match tokio::time::timeout(PROBE_TIMEOUT, query).await {
                Ok(Ok(_)) => DepStatus::up(start.elapsed()),
                Ok(Err(e)) => DepStatus::down(e.to_string()),
                Err(_) => DepStatus::down("probe timed out"),
            }
        }
    };

    // Redis: fresh connection + `PING`. Independent of cluster mode so the panel
    // reports reachability even when we aren't bridging through it.
    let redis = {
        let start = Instant::now();
        match redis::Client::open(&*state.redis_url) {
            Err(e) => DepStatus::down(format!("bad REDIS_URL: {e}")),
            Ok(client) => {
                let ping = async {
                    let mut conn = client.get_multiplexed_async_connection().await?;
                    redis::cmd("PING").query_async::<String>(&mut conn).await
                };
                match timeout(PROBE_TIMEOUT, ping).await {
                    Ok(Ok(_)) => DepStatus::up(start.elapsed()),
                    Ok(Err(e)) => DepStatus::down(e.to_string()),
                    Err(_) => DepStatus::down("probe timed out"),
                }
            }
        }
    };

    Json(DepsHealth {
        db,
        redis,
        cluster_mode: state.cluster.is_some(),
        ws_auth_configured: !state.ws_auth_token.is_empty(),
    })
}

/// `POST /admin/people` body. The avatar is a Notion-style emoji on a background
/// color; both default if the client omits them.
#[derive(Debug, Deserialize)]
struct NewPerson {
    name: String,
    #[serde(default = "default_person_emoji")]
    emoji: String,
    #[serde(default = "default_color")]
    color: String,
}

/// `POST /admin/groups` body. A group's `name` is the topic sockets subscribe to;
/// `emoji` + `color` are its avatar.
#[derive(Debug, Deserialize)]
struct NewGroup {
    name: String,
    #[serde(default = "default_group_emoji")]
    emoji: String,
    #[serde(default = "default_color")]
    color: String,
}

fn default_color() -> String {
    "#6366f1".to_string()
}

fn default_person_emoji() -> String {
    "🧘".to_string()
}

fn default_group_emoji() -> String {
    "🎨".to_string()
}

async fn list_people(State(state): State<AppState>) -> Result<Json<Vec<Person>>, AppError> {
    Ok(Json(directory(&state)?.list_people().await?))
}

async fn create_person(
    State(state): State<AppState>,
    Json(body): Json<NewPerson>,
) -> Result<Json<Person>, AppError> {
    let name = body.name.trim();
    if name.is_empty() {
        return Err(AppError::BadRequest("name must not be empty".into()));
    }
    Ok(Json(
        directory(&state)?
            .create_person(name, &body.emoji, &body.color)
            .await?,
    ))
}

async fn delete_person(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<StatusCode, AppError> {
    directory(&state)?.delete_person(id).await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn list_groups(State(state): State<AppState>) -> Result<Json<Vec<Group>>, AppError> {
    Ok(Json(directory(&state)?.list_groups().await?))
}

async fn create_group(
    State(state): State<AppState>,
    Json(body): Json<NewGroup>,
) -> Result<Json<Group>, AppError> {
    let name = body.name.trim();
    if name.is_empty() {
        return Err(AppError::BadRequest("group name must not be empty".into()));
    }
    Ok(Json(
        directory(&state)?
            .create_group(name, &body.emoji, &body.color)
            .await?,
    ))
}

async fn add_member(
    State(state): State<AppState>,
    Path((person_id, group_id)): Path<(Uuid, Uuid)>,
) -> Result<StatusCode, AppError> {
    directory(&state)?.add_member(person_id, group_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn remove_member(
    State(state): State<AppState>,
    Path((person_id, group_id)): Path<(Uuid, Uuid)>,
) -> Result<StatusCode, AppError> {
    directory(&state)?
        .remove_member(person_id, group_id)
        .await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn list_memberships(
    State(state): State<AppState>,
) -> Result<Json<Vec<Membership>>, AppError> {
    Ok(Json(directory(&state)?.memberships().await?))
}

/// The `/metrics` scrape endpoint, kept separate from [`router`] because it
/// closes over the Prometheus [`PrometheusHandle`] instead of `AppState` — the
/// recorder is installed once in `main`, never in tests. Public and unauthed,
/// like `/healthz`: a scraper reaches it without an API key.
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

/// Query params accepted on `GET /ws`. A browser `WebSocket` can't set custom
/// headers on the handshake, so the shared secret rides the query string
/// instead — the one channel a browser client actually controls pre-upgrade.
#[derive(Debug, Deserialize)]
struct WsAuthQuery {
    token: Option<String>,
    /// Display identity the client claims (e.g. the person's name from the admin
    /// panel). **Never trusted for anything but display** (SPEC security) —
    /// sanitized and capped below, and it only ever feeds the presence roster.
    /// Absent → we fall back to the connection id.
    identity: Option<String>,
}

/// Client display identities are capped so a client can't wedge the presence map
/// with an absurd key (SPEC: cap everything a client controls).
const MAX_IDENTITY_LEN: usize = 64;

/// Sanitize the client-claimed identity: trim, cap length, fall back to the
/// connection id when missing or blank. Display-only — never an authz input.
fn resolve_identity(claimed: Option<String>, conn: ConnId) -> String {
    claimed
        .map(|s| s.trim().chars().take(MAX_IDENTITY_LEN).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| conn.to_string())
}

/// The WebSocket upgrade. Authenticates *before* accepting — a missing or
/// wrong `?token=` is rejected with `401` and the socket never opens. Only
/// once that check passes do we return `101 Switching Protocols` and hand the
/// open socket to [`handle_socket`].
async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
    Query(query): Query<WsAuthQuery>,
) -> Result<Response, AppError> {
    let provided = query.token.unwrap_or_default();
    // Empty `ws_auth_token` means the server itself is misconfigured (no
    // secret set) — fail closed rather than treat that as "auth disabled".
    if state.ws_auth_token.is_empty() || provided != *state.ws_auth_token {
        return Err(AppError::Unauthorized);
    }
    Ok(ws.on_upgrade(move |socket| handle_socket(socket, state, query.identity)))
}

/// Drive one connection for its whole lifetime: a writer task drains this
/// connection's outbox to the socket; this loop reads client frames and
/// dispatches them. On *any* exit, the connection is removed from the hub and
/// presence so nothing is left dangling.
async fn handle_socket(socket: WebSocket, state: AppState, identity: Option<String>) {
    let conn = ConnId::next();
    // The person's display name (from the admin panel), sanitized. This is what
    // the presence roster shows — so a socket opened for "alice" appears as
    // alice, not conn-7.
    let identity = resolve_identity(identity, conn);
    info!(%conn, %identity, "websocket connected");

    let (mut ws_tx, mut ws_rx) = socket.split();
    let (mailbox, mut outbox) = backpressure::mailbox(state.outbox_capacity, state.overflow_policy);

    // Writer task: serialize each queued ServerMessage and push it to the socket.
    // Backpressure is upstream (the bounded mailbox); this just forwards.
    let mut writer = tokio::spawn(async move {
        while let Some(msg) = outbox.recv().await {
            let json = match serde_json::to_string(&msg) {
                Ok(j) => j,
                Err(e) => {
                    warn!(error = %e, "failed to encode server message");
                    continue;
                }
            };
            if ws_tx.send(Message::Text(json.into())).await.is_err() {
                break; // socket closed under us
            }
        }
    });

    // Reader loop: client -> server frames.
    loop {
        tokio::select! {
            frame = ws_rx.next() => match frame {
                Some(Ok(Message::Text(text))) => {
                    match serde_json::from_str::<ClientMessage>(text.as_str()) {
                        Ok(cmd) => dispatch(&state, conn, &identity, &mailbox, cmd).await,
                        Err(e) => {
                            let _ = mailbox.deliver(ServerMessage::Error {
                                reason: format!("invalid message: {e}"),
                            });
                        }
                    }
                }
                Some(Ok(Message::Close(_))) | None => break,
                Some(Ok(_)) => {} // ping/pong/binary: TODO(protocol) handle heartbeats
                Some(Err(e)) => {
                    debug!(%conn, error = %e, "websocket receive error");
                    break;
                }
            },
            // Writer finished first (socket closed on the send side): we're done.
            _ = &mut writer => break,
        }
    }

    // Teardown — runs no matter how we exited (clean close, error, abrupt drop).
    state.hub.disconnect(conn);
    state.presence.disconnect(conn);
    writer.abort();
    info!(%conn, "websocket disconnected");
}

/// Apply one decoded client command.
async fn dispatch(
    state: &AppState,
    conn: ConnId,
    identity: &str,
    mailbox: &Mailbox,
    cmd: ClientMessage,
) {
    match cmd {
        ClientMessage::Subscribe { topic } => {
            state.hub.subscribe(&topic, conn, mailbox.clone());
            state.presence.join(&topic, conn, identity.to_string());
            state.hub.publish(&topic, presence_snapshot(state, &topic));
        }
        ClientMessage::Unsubscribe { topic } => {
            state.hub.unsubscribe(&topic, conn);
            state.presence.leave(&topic, conn);
            state.hub.publish(&topic, presence_snapshot(state, &topic));
        }
        ClientMessage::Publish { topic, payload } => {
            let msg = ServerMessage::Message {
                topic: topic.clone(),
                payload: payload.clone(),
            };
            state.hub.publish(&topic, msg);
            if let Some(cluster) = &state.cluster {
                cluster.publish(&topic, &payload).await;
            }
        }
        ClientMessage::Heartbeat => {
            state.presence.touch(conn);
        }
    }
}

/// Snapshot `topic`'s current roster as a `ServerMessage::Presence`.
///
/// A full roster, not a "someone joined/left" delta: presence messages ride
/// the same bounded mailbox as everything else (V2), so they can be dropped
/// under backpressure. A snapshot self-heals on the next join/leave; a delta
/// would leave a client permanently wrong about who's in the room.
fn presence_snapshot(state: &AppState, topic: &str) -> ServerMessage {
    let members = state
        .presence
        .members(topic)
        .into_iter()
        .map(|m| m.identity().to_string())
        .collect();
    ServerMessage::Presence {
        topic: topic.to_string(),
        members,
    }
}
