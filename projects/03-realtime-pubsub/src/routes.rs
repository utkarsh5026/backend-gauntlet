//! HTTP surface + the per-connection WebSocket loop.
//!
//! The wiring here is complete: the router, the `GET /ws` upgrade, splitting the
//! socket into a reader and a writer task, parsing client frames, and tearing the
//! connection down on every exit path. What it *calls into* — the hub, the
//! mailbox's overflow policy, presence, the cluster bridge — is where the SPEC's
//! `todo!()`s live. Run this as-is and a publish will panic with "V1: fan-out…",
//! which is exactly the worklist.

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::State;
use axum::response::Response;
use axum::routing::get;
use axum::Router;
use futures_util::{SinkExt, StreamExt};
use metrics_exporter_prometheus::PrometheusHandle;
use tower_http::trace::TraceLayer;
use tracing::{debug, info, warn};

use crate::backpressure::{self, Mailbox};
use crate::protocol::{ClientMessage, ConnId, ServerMessage};
use crate::AppState;

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/ws", get(ws_handler))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
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

/// The WebSocket upgrade. Returns `101 Switching Protocols`, then hands the open
/// socket to [`handle_socket`].
///
/// TODO(security): authenticate *before* accepting the upgrade — pull an API
/// key / token from the query string or a header and reject with `401` here, so
/// anonymous clients can never open a socket. (See `error::AppError::Unauthorized`.)
async fn ws_handler(ws: WebSocketUpgrade, State(state): State<AppState>) -> Response {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

/// Drive one connection for its whole lifetime: a writer task drains this
/// connection's outbox to the socket; this loop reads client frames and
/// dispatches them. On *any* exit, the connection is removed from the hub and
/// presence so nothing is left dangling.
async fn handle_socket(socket: WebSocket, state: AppState) {
    let conn = ConnId::next();
    info!(%conn, "websocket connected");

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
                        Ok(cmd) => dispatch(&state, conn, &mailbox, cmd).await,
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
async fn dispatch(state: &AppState, conn: ConnId, mailbox: &Mailbox, cmd: ClientMessage) {
    match cmd {
        ClientMessage::Subscribe { topic } => {
            state.hub.subscribe(&topic, conn, mailbox.clone());
            // TODO(security): derive the display identity from the authenticated
            // token, not from the client — `conn` is a safe placeholder for now.
            state.presence.join(&topic, conn, conn.to_string());
            // TODO(V3): broadcast the updated presence to the room here.
        }
        ClientMessage::Unsubscribe { topic } => {
            state.hub.unsubscribe(&topic, conn);
            state.presence.leave(&topic, conn);
        }
        ClientMessage::Publish { topic, payload } => {
            // Deliver to this node's local subscribers (V1)…
            let msg = ServerMessage::Message {
                topic: topic.clone(),
                payload: payload.clone(),
            };
            state.hub.publish(&topic, msg);
            // …and onto the cross-node bus so other nodes' subscribers get it (V4).
            // The receive side must NOT re-deliver locally-originated messages —
            // see `cluster::ClusterBridge::run`'s loop-break.
            if let Some(cluster) = &state.cluster {
                cluster.publish(&topic, &payload).await;
            }
        }
        ClientMessage::Heartbeat => {
            state.presence.touch(conn);
        }
    }
}
