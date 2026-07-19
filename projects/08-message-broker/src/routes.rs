//! HTTP surface: produce, fetch, topic admin, and consumer-group offsets.
//!
//! The routing, request/response shapes, and the size/validation guards are
//! wired. What the handlers call into — `topic.produce` (V3 → V1), `partition
//! .read_from` (V1 → V2), `groups.commit`/`join` (V4) — is where the `todo!()`s
//! live. Run as-is and `GET /healthz` works; the first real produce/fetch/join
//! panics with a Vx todo, which is the worklist.
//!
//! Record bytes are carried as UTF-8 strings over JSON for now — simple to
//! `curl`. Switching to base64 (arbitrary bytes) or a binary TCP framing is the
//! "wire format" horizontal item (see SPEC).

use axum::extract::{Path, Query, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use bytes::Bytes;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::record::Record;
use crate::AppState;

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        // Topic admin.
        .route("/topics", post(create_topic).get(list_topics))
        // Produce a batch to a topic.
        .route("/topics/{topic}/records", post(produce))
        // Fetch a batch from a partition, starting at an offset.
        .route("/topics/{topic}/partitions/{partition}/records", get(fetch))
        // Consumer groups (V4): membership + offset commits.
        .route("/groups/{group}/members", post(join_group))
        .route(
            "/groups/{group}/members/{member}",
            axum::routing::delete(leave_group),
        )
        .route(
            "/groups/{group}/offsets",
            post(commit_offset).get(fetch_offset),
        )
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok"
}

// ---- Topic admin -----------------------------------------------------------

#[derive(Debug, Deserialize)]
struct CreateTopicReq {
    name: String,
    /// Partition count; `None` uses the broker's configured default.
    partitions: Option<u32>,
}

/// `POST /topics` — create a topic (V3).
async fn create_topic(
    State(state): State<AppState>,
    Json(req): Json<CreateTopicReq>,
) -> Result<Json<serde_json::Value>, AppError> {
    let topic = state.broker.create_topic(&req.name, req.partitions).await?;
    Ok(Json(serde_json::json!({
        "name": topic.name(),
        "partitions": topic.partition_count(),
    })))
}

/// `GET /topics` — list topics.
async fn list_topics(State(state): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "topics": state.broker.list_topics().await }))
}

// ---- Produce ---------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct ProduceReq {
    records: Vec<RecordIn>,
}

#[derive(Debug, Deserialize)]
struct RecordIn {
    /// Optional partition key (V3): same key → same partition.
    key: Option<String>,
    value: String,
    /// Optional producer timestamp (epoch millis); stamped now if absent.
    timestamp: Option<i64>,
}

#[derive(Debug, Serialize)]
struct ProduceResult {
    partition: u32,
    offset: u64,
}

/// `POST /topics/{topic}/records` — produce a batch (V3 partitioner → V1 append).
///
/// TODO(security): authenticate this before doing anything — an open produce
/// endpoint is an open disk for the whole internet.
async fn produce(
    State(state): State<AppState>,
    Path(topic): Path<String>,
    Json(req): Json<ProduceReq>,
) -> Result<Json<serde_json::Value>, AppError> {
    let topic = state.broker.topic(&topic).await?;

    let mut results = Vec::with_capacity(req.records.len());
    for r in req.records {
        // Enforce the per-record size cap (security horizontal) before storing.
        if r.value.len() as u64 > state.max_record_bytes {
            return Err(AppError::RecordTooLarge);
        }
        let record = Record {
            key: r.key.map(Bytes::from),
            value: Bytes::from(r.value),
            timestamp: r.timestamp.unwrap_or_else(|| Utc::now().timestamp_millis()),
        };
        let (partition, offset) = topic.produce(record).await?;
        results.push(ProduceResult { partition, offset });
    }

    Ok(Json(serde_json::json!({ "results": results })))
}

// ---- Fetch -----------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct FetchQuery {
    #[serde(default)]
    offset: u64,
    /// Bound the batch so a fetch can't be asked to return the whole log.
    #[serde(default = "default_max_records")]
    max_records: usize,
}

fn default_max_records() -> usize {
    100
}

#[derive(Debug, Serialize)]
struct RecordOut {
    offset: u64,
    timestamp: i64,
    key: Option<String>,
    value: String,
}

/// `GET /topics/{topic}/partitions/{p}/records?offset=&max_records=` — fetch a
/// bounded batch starting at `offset`, plus the `next_offset` to continue from
/// (V1 read + V2 seek).
async fn fetch(
    State(state): State<AppState>,
    Path((topic, partition)): Path<(String, u32)>,
    Query(q): Query<FetchQuery>,
) -> Result<Json<serde_json::Value>, AppError> {
    let topic = state.broker.topic(&topic).await?;
    let partition = topic.partition(partition)?;

    let records = partition.read_from(q.offset, q.max_records).await?;
    // Continue from just past the last returned record; if the batch was empty
    // (caught up to the log end), stay put at the requested offset.
    let next_offset = records.last().map(|r| r.offset + 1).unwrap_or(q.offset);

    let out: Vec<RecordOut> = records
        .into_iter()
        .map(|r| RecordOut {
            offset: r.offset,
            timestamp: r.timestamp,
            key: r.key.map(|k| String::from_utf8_lossy(&k).into_owned()),
            value: String::from_utf8_lossy(&r.value).into_owned(),
        })
        .collect();

    Ok(Json(serde_json::json!({
        "records": out,
        "next_offset": next_offset,
    })))
}

// ---- Consumer groups (V4) --------------------------------------------------

#[derive(Debug, Deserialize)]
struct JoinReq {
    member_id: String,
    topic: String,
}

/// `POST /groups/{group}/members` — join a group; returns the assigned partitions.
async fn join_group(
    State(state): State<AppState>,
    Path(group): Path<String>,
    Json(req): Json<JoinReq>,
) -> Result<Json<serde_json::Value>, AppError> {
    let topic = state.broker.topic(&req.topic).await?;
    let assignment = state
        .broker
        .groups()
        .join(
            &group,
            &req.member_id,
            &req.topic,
            topic.partition_count() as u32,
        )
        .await?;
    Ok(Json(serde_json::json!({
        "member_id": req.member_id,
        "assignment": assignment.partitions,
    })))
}

/// `DELETE /groups/{group}/members/{member}` — leave a group (triggers rebalance).
async fn leave_group(
    State(state): State<AppState>,
    Path((group, member)): Path<(String, String)>,
) -> Result<axum::http::StatusCode, AppError> {
    state.broker.groups().leave(&group, &member).await?;
    Ok(axum::http::StatusCode::NO_CONTENT)
}

#[derive(Debug, Deserialize)]
struct CommitReq {
    topic: String,
    partition: u32,
    offset: u64,
}

/// `POST /groups/{group}/offsets` — durably commit the group's progress (V4).
async fn commit_offset(
    State(state): State<AppState>,
    Path(group): Path<String>,
    Json(req): Json<CommitReq>,
) -> Result<axum::http::StatusCode, AppError> {
    state
        .broker
        .groups()
        .commit(&group, &req.topic, req.partition, req.offset)
        .await?;
    Ok(axum::http::StatusCode::NO_CONTENT)
}

#[derive(Debug, Deserialize)]
struct OffsetQuery {
    topic: String,
    partition: u32,
}

/// `GET /groups/{group}/offsets?topic=&partition=` — the group's committed offset.
async fn fetch_offset(
    State(state): State<AppState>,
    Path(group): Path<String>,
    Query(q): Query<OffsetQuery>,
) -> Result<Json<serde_json::Value>, AppError> {
    let committed = state
        .broker
        .groups()
        .committed(&group, &q.topic, q.partition)
        .await?;
    Ok(Json(serde_json::json!({
        "group": group,
        "topic": q.topic,
        "partition": q.partition,
        "committed": committed,
    })))
}
