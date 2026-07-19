//! HTTP surface: create accounts, move money, read balances/transactions.
//!
//! The router, extractors, and the idempotency + response *wiring* are done. What the
//! handlers call into — `ledger.create_account`, `isolation::transfer`,
//! `idempotency.lookup_or_reserve` — is where the `todo!()`s live. Run the bare
//! scaffold and `POST /accounts` panics with "V1: create an account", `POST /transfers`
//! panics in the transfer/idempotency path — those messages are the worklist.

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::idempotency::{IdempotencyStore, KeyState, StoredResponse};
use crate::isolation;
use crate::money::{AccountId, NewAccount, NewTransfer, TxId};
use crate::AppState;

/// The `Idempotency-Key` request header (V3).
const IDEMPOTENCY_HEADER: &str = "idempotency-key";

/// Build the application router (everything except `/metrics`, which closes over the
/// Prometheus handle instead of `AppState` — see [`metrics_router`]).
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/accounts", post(create_account))
        .route("/accounts/{id}/balance", get(get_balance))
        .route("/transfers", post(create_transfer))
        .route("/transactions/{id}", get(get_transaction))
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

/// `POST /accounts` — create an account (V1).
///
/// TODO(security): require a valid API key before creating anything.
async fn create_account(
    State(state): State<AppState>,
    Json(new): Json<NewAccount>,
) -> Result<(StatusCode, Json<serde_json::Value>), AppError> {
    let account = state.ledger.create_account(new).await?;
    let body = serde_json::to_value(account).map_err(|e| AppError::Other(e.into()))?;
    Ok((StatusCode::CREATED, Json(body)))
}

/// `GET /accounts/{id}/balance` — the account's derived balance (V1).
async fn get_balance(
    State(state): State<AppState>,
    Path(id): Path<AccountId>,
) -> Result<Json<serde_json::Value>, AppError> {
    // Confirm the account exists so an unknown id is a clean 404, not a silent 0.
    state
        .ledger
        .get_account(id)
        .await?
        .ok_or(AppError::NotFound)?;
    let balance = state.ledger.balance(id).await?;
    let body = serde_json::to_value(balance).map_err(|e| AppError::Other(e.into()))?;
    Ok(Json(body))
}

/// `GET /transactions/{id}` — a posted transaction and its entries (V1).
async fn get_transaction(
    State(state): State<AppState>,
    Path(id): Path<TxId>,
) -> Result<Json<serde_json::Value>, AppError> {
    let txn = state
        .ledger
        .get_transaction(id)
        .await?
        .ok_or(AppError::NotFound)?;
    let body = serde_json::to_value(txn).map_err(|e| AppError::Other(e.into()))?;
    Ok(Json(body))
}

/// `POST /transfers` — move money A → B (V2), deduped on `Idempotency-Key` (V3).
///
/// The handler is the *consumer* of both verticals: it dedupes on the key, executes
/// the concurrency-safe transfer, and stores the result for future replays. The hard
/// parts it calls (`fingerprint` / `lookup_or_reserve` / `store` in V3, `transfer` in
/// V2) are `todo!()`. Status codes are deliberate: `201` on a fresh post, `200` on an
/// idempotent replay, `409` on a key conflict.
///
/// TODO(security): require a valid API key before moving any money. Reads the raw body
/// as [`Bytes`] (not `Json`) so the idempotency fingerprint sees the exact request.
async fn create_transfer(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, AppError> {
    let new: NewTransfer = serde_json::from_slice(&body)
        .map_err(|e| AppError::BadRequest(format!("invalid transfer body: {e}")))?;

    // A client that sends an Idempotency-Key opts into dedupe; one that doesn't owns
    // its own double-charge risk (that's the point of making the key explicit).
    let idem_key = headers
        .get(IDEMPOTENCY_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);

    if let Some(key) = idem_key.as_deref() {
        let fingerprint = IdempotencyStore::fingerprint(&body);
        match state
            .idempotency
            .lookup_or_reserve(key, &fingerprint)
            .await?
        {
            // Already served this key — replay the stored response, as a 200.
            KeyState::Replay(stored) => return Ok(replay_response(stored)),
            KeyState::Mismatch => {
                return Err(AppError::IdempotencyConflict(
                    "key reused with a different request body".into(),
                ))
            }
            KeyState::InProgress => {
                return Err(AppError::IdempotencyConflict(
                    "a request with this key is still in progress".into(),
                ))
            }
            // We reserved the key; fall through and execute (then store the result).
            KeyState::Fresh => {}
        }
    }

    let outcome = isolation::transfer(&state.ledger, &state.transfer_cfg, new).await?;
    let value =
        serde_json::to_value(&outcome.transaction).map_err(|e| AppError::Other(e.into()))?;

    if let Some(key) = idem_key.as_deref() {
        let stored = StoredResponse {
            status_code: StatusCode::CREATED.as_u16(),
            transaction_id: Some(outcome.transaction.id),
            body: value.clone(),
        };
        state.idempotency.store(key, &stored).await?;
    }

    Ok((StatusCode::CREATED, Json(value)).into_response())
}

/// Render a stored idempotency result. A replay is a `200` (the resource already
/// existed), not the original `201`.
fn replay_response(stored: StoredResponse) -> Response {
    let status = StatusCode::from_u16(stored.status_code).unwrap_or(StatusCode::OK);
    let status = if status == StatusCode::CREATED {
        StatusCode::OK
    } else {
        status
    };
    (status, Json(stored.body)).into_response()
}
