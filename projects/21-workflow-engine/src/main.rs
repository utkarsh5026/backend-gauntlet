//! Workflow engine (Temporal-lite) — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the Postgres pool, the gRPC frontend, the
//! `/metrics` sidecar, the durable-timer background loop, graceful shutdown) is wired
//! up for you. The learning lives in the modules marked `TODO(Vx)`:
//!   - V1 `history.rs`  — the append-only event log (the state IS the log).
//!   - V2 `replay.rs`   — folding a history into state, deterministically.
//!   - V3 `timers.rs`   — durable timers that survive a restart.
//!   - V4 `dispatch.rs` — task queues, long-poll, at-least-once worker dispatch.
//!   - V5 `sticky.rs`   — worker-affinity cache that skips full replay.
//! See SPEC.md.
//!
//! Scaffold state: this compiles and serves the gRPC + metrics endpoints. Every RPC
//! `todo!()`-panics on its first call (that panic message is the worklist), and turning
//! on `RUN_TIMER_SERVICE` makes the timer loop panic on its first scan. The gRPC
//! *adapter* below is complete — it marshals protobuf ⇄ the internal model and delegates
//! to the [`Dispatcher`]; only the engine methods it calls are unimplemented.

mod dispatch;
mod error;
mod history;
mod metrics;
mod model;
mod replay;
mod sticky;
mod timers;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use sqlx::postgres::PgPoolOptions;
use tokio::sync::watch;
use tonic::transport::Server;
use tonic::{Request, Response, Status};
use tracing::info;

use dispatch::{Dispatcher, DispatcherConfig};
use history::{HistoryStore, StartOptions};
use model::{Command, Event, EventType, ExecutionStatus, RunId, TaskToken};
use sticky::StickyCache;
use timers::TimerService;

/// The generated protobuf types + service traits.
pub mod pb {
    tonic::include_proto!("workflow.v1");
}

use pb::workflow_service_server::{WorkflowService, WorkflowServiceServer};
use pb::{
    Command as PbCommand, CommandType, GetWorkflowResultRequest, GetWorkflowResultResponse,
    HistoryEvent as PbHistoryEvent, PollActivityTaskRequest, PollActivityTaskResponse,
    PollWorkflowTaskRequest, PollWorkflowTaskResponse, RespondActivityTaskCompletedRequest,
    RespondActivityTaskCompletedResponse, RespondActivityTaskFailedRequest,
    RespondActivityTaskFailedResponse, RespondWorkflowTaskCompletedRequest,
    RespondWorkflowTaskCompletedResponse, StartWorkflowRequest, StartWorkflowResponse,
};

const DEFAULT_GRPC_PORT: u16 = 7233;
const DEFAULT_METRICS_PORT: u16 = 9090;

/// The gRPC service: a thin adapter from protobuf messages to the [`Dispatcher`] and
/// back. All the interesting logic is behind the engine modules.
pub struct WorkflowSvc {
    dispatcher: Arc<Dispatcher>,
}

#[tonic::async_trait]
impl WorkflowService for WorkflowSvc {
    async fn start_workflow(
        &self,
        request: Request<StartWorkflowRequest>,
    ) -> Result<Response<StartWorkflowResponse>, Status> {
        let req = request.into_inner();
        if req.task_queue.is_empty() {
            return Err(Status::invalid_argument("task_queue must not be empty"));
        }
        let run_id = self
            .dispatcher
            .start_workflow(StartOptions {
                workflow_id: req.workflow_id,
                workflow_type: req.workflow_type,
                task_queue: req.task_queue,
                input: req.input,
            })
            .await?;
        Ok(Response::new(StartWorkflowResponse {
            run_id: run_id.to_string(),
        }))
    }

    async fn poll_workflow_task(
        &self,
        request: Request<PollWorkflowTaskRequest>,
    ) -> Result<Response<PollWorkflowTaskResponse>, Status> {
        let req = request.into_inner();
        match self
            .dispatcher
            .poll_workflow_task(&req.task_queue, &req.identity)
            .await?
        {
            Some(task) => Ok(Response::new(PollWorkflowTaskResponse {
                task_token: task.token.encode(),
                workflow_id: task.workflow_id,
                run_id: task.run_id.to_string(),
                history: task.history.iter().map(to_pb_event).collect(),
                sticky_cache_hit: task.sticky_cache_hit,
            })),
            // Empty response = the long-poll timed out; the worker polls again.
            None => Ok(Response::new(PollWorkflowTaskResponse::default())),
        }
    }

    async fn respond_workflow_task_completed(
        &self,
        request: Request<RespondWorkflowTaskCompletedRequest>,
    ) -> Result<Response<RespondWorkflowTaskCompletedResponse>, Status> {
        let req = request.into_inner();
        let token = TaskToken::decode(&req.task_token)
            .ok_or_else(|| Status::invalid_argument("malformed task token"))?;
        let identity = req_identity(&token);
        let commands = decode_commands(req.commands)?;
        self.dispatcher
            .complete_workflow_task(token, &identity, commands)
            .await?;
        Ok(Response::new(RespondWorkflowTaskCompletedResponse {}))
    }

    async fn poll_activity_task(
        &self,
        request: Request<PollActivityTaskRequest>,
    ) -> Result<Response<PollActivityTaskResponse>, Status> {
        let req = request.into_inner();
        match self
            .dispatcher
            .poll_activity_task(&req.task_queue, &req.identity)
            .await?
        {
            Some(task) => Ok(Response::new(PollActivityTaskResponse {
                task_token: task.token.encode(),
                activity_type: task.activity_type,
                input: task.input,
                workflow_id: task.workflow_id,
                run_id: task.run_id.to_string(),
            })),
            None => Ok(Response::new(PollActivityTaskResponse::default())),
        }
    }

    async fn respond_activity_task_completed(
        &self,
        request: Request<RespondActivityTaskCompletedRequest>,
    ) -> Result<Response<RespondActivityTaskCompletedResponse>, Status> {
        let req = request.into_inner();
        let token = TaskToken::decode(&req.task_token)
            .ok_or_else(|| Status::invalid_argument("malformed task token"))?;
        self.dispatcher
            .complete_activity_task(token, req.result)
            .await?;
        Ok(Response::new(RespondActivityTaskCompletedResponse {}))
    }

    async fn respond_activity_task_failed(
        &self,
        request: Request<RespondActivityTaskFailedRequest>,
    ) -> Result<Response<RespondActivityTaskFailedResponse>, Status> {
        let req = request.into_inner();
        let token = TaskToken::decode(&req.task_token)
            .ok_or_else(|| Status::invalid_argument("malformed task token"))?;
        self.dispatcher
            .fail_activity_task(token, req.failure)
            .await?;
        Ok(Response::new(RespondActivityTaskFailedResponse {}))
    }

    async fn get_workflow_result(
        &self,
        request: Request<GetWorkflowResultRequest>,
    ) -> Result<Response<GetWorkflowResultResponse>, Status> {
        let req = request.into_inner();
        let run_id: RunId = req
            .run_id
            .parse()
            .map_err(|_| Status::invalid_argument("run_id is not a valid uuid"))?;
        let state = self.dispatcher.get_result(run_id).await?;
        Ok(Response::new(GetWorkflowResultResponse {
            running: state.status == ExecutionStatus::Running,
            completed: state.status == ExecutionStatus::Completed,
            result: state.result.unwrap_or_default(),
            failure: state.failure.unwrap_or_default(),
        }))
    }
}

// ---- protobuf ⇄ model marshaling (wiring, not logic) ----

/// Render an internal [`Event`] onto the wire.
fn to_pb_event(e: &Event) -> PbHistoryEvent {
    PbHistoryEvent {
        event_id: e.event_id,
        event_type: event_type_to_pb(e.event_type) as i32,
        timestamp_ms: e.timestamp_ms,
        attributes: serde_json::to_vec(&e.attributes).unwrap_or_default(),
    }
}

fn event_type_to_pb(t: EventType) -> pb::EventType {
    match t {
        EventType::WorkflowStarted => pb::EventType::WorkflowStarted,
        EventType::WorkflowTaskScheduled => pb::EventType::WorkflowTaskScheduled,
        EventType::WorkflowTaskStarted => pb::EventType::WorkflowTaskStarted,
        EventType::WorkflowTaskCompleted => pb::EventType::WorkflowTaskCompleted,
        EventType::ActivityScheduled => pb::EventType::ActivityScheduled,
        EventType::ActivityStarted => pb::EventType::ActivityStarted,
        EventType::ActivityCompleted => pb::EventType::ActivityCompleted,
        EventType::ActivityFailed => pb::EventType::ActivityFailed,
        EventType::TimerStarted => pb::EventType::TimerStarted,
        EventType::TimerFired => pb::EventType::TimerFired,
        EventType::WorkflowCompleted => pb::EventType::WorkflowCompleted,
        EventType::WorkflowFailed => pb::EventType::WorkflowFailed,
    }
}

/// Decode the wire commands a worker returned into the internal [`Command`] enum,
/// rejecting anything malformed before it reaches the engine.
fn decode_commands(commands: Vec<PbCommand>) -> Result<Vec<Command>, Status> {
    commands.into_iter().map(decode_command).collect()
}

fn decode_command(c: PbCommand) -> Result<Command, Status> {
    let kind = CommandType::try_from(c.command_type)
        .map_err(|_| Status::invalid_argument("unknown command type"))?;
    Ok(match kind {
        CommandType::ScheduleActivity => {
            if c.activity_type.is_empty() {
                return Err(Status::invalid_argument("activity_type must not be empty"));
            }
            Command::ScheduleActivity {
                activity_type: c.activity_type,
                input: c.activity_input,
            }
        }
        CommandType::StartTimer => {
            if c.timer_id.is_empty() {
                return Err(Status::invalid_argument("timer_id must not be empty"));
            }
            Command::StartTimer {
                timer_id: c.timer_id,
                delay_ms: c.timer_delay_ms,
            }
        }
        CommandType::CompleteWorkflow => Command::CompleteWorkflow { result: c.result },
        CommandType::FailWorkflow => Command::FailWorkflow { failure: c.failure },
        CommandType::Unspecified => {
            return Err(Status::invalid_argument("command type is unspecified"))
        }
    })
}

/// The worker identity that owns a task token's execution. The token doesn't carry the
/// poller's identity; the engine tracks it via the claim (`task_queue.locked_by`). For
/// the sticky pin refresh we derive a stable key from the run — the dispatcher resolves
/// the real worker identity from the claim it recorded at poll time.
fn req_identity(token: &TaskToken) -> String {
    token.run_id.to_string()
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,workflow_engine=debug,sqlx=warn");

    let grpc_port: u16 = common_config::parse_or("PORT", DEFAULT_GRPC_PORT);
    let metrics_port: u16 = common_config::parse_or("METRICS_PORT", DEFAULT_METRICS_PORT);
    let database_url = common_config::require("DATABASE_URL")?;
    let db_max_connections: u32 = common_config::parse_or("DB_MAX_CONNECTIONS", 20);

    let cfg = DispatcherConfig {
        long_poll_timeout: Duration::from_millis(common_config::parse_or(
            "LONG_POLL_TIMEOUT_MS",
            5_000u64,
        )),
        visibility_timeout: Duration::from_millis(common_config::parse_or(
            "TASK_VISIBILITY_TIMEOUT_MS",
            30_000u64,
        )),
    };
    let sticky_ttl = Duration::from_millis(common_config::parse_or("STICKY_TTL_MS", 10_000u64));

    // Postgres: the durable source of truth for history, task queues, and timers.
    let pool = PgPoolOptions::new()
        .max_connections(db_max_connections)
        .connect(&database_url)
        .await?;
    info!("connected to postgres");

    // Prometheus recorder + a handle to render `/metrics`.
    let metrics_handle = metrics::install();

    // Assemble the engine. Everything shares the one pool; the heavy handles are behind
    // `Arc`, so the gRPC service and the timer loop can both hold them.
    let history = Arc::new(HistoryStore::new(pool.clone()));
    let timers = Arc::new(TimerService::new(pool.clone()));
    let sticky = Arc::new(StickyCache::new(sticky_ttl));
    let dispatcher = Dispatcher::new(pool.clone(), history, timers.clone(), sticky, cfg);

    // Graceful shutdown is broadcast to every server/task via this watch.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    tokio::spawn({
        let shutdown_tx = shutdown_tx.clone();
        async move {
            let _ = tokio::signal::ctrl_c().await;
            info!("shutdown signal received");
            let _ = shutdown_tx.send(true);
        }
    });

    let mut tasks = Vec::new();

    // The `/metrics` + `/healthz` sidecar, on its own HTTP port beside the gRPC frontend.
    {
        let router = metrics::observability_router(metrics_handle);
        let addr: SocketAddr = format!("0.0.0.0:{metrics_port}").parse()?;
        let listener = tokio::net::TcpListener::bind(addr).await?;
        info!(%addr, "observability sidecar listening (/metrics, /healthz)");
        let shutdown = shutdown_rx.clone();
        tasks.push(tokio::spawn(async move {
            let _ = axum::serve(listener, router)
                .with_graceful_shutdown(wait_for_shutdown(shutdown))
                .await;
        }));
    }

    // The durable-timer scan loop runs only when asked, so the bare scaffold serves
    // cleanly. (Its first scan is a V3 `todo!()` — flip RUN_TIMER_SERVICE=true once V3
    // works.)
    if common_config::parse_or("RUN_TIMER_SERVICE", false) {
        let interval =
            Duration::from_millis(common_config::parse_or("TIMER_SCAN_INTERVAL_MS", 200u64));
        tasks.push(tokio::spawn(timers::scan_loop(
            timers,
            dispatcher.clone(),
            interval,
            shutdown_rx.clone(),
        )));
        info!("durable timer service started");
    } else {
        info!("durable timer service disabled (RUN_TIMER_SERVICE=false)");
    }

    let svc = WorkflowSvc { dispatcher };
    let grpc_addr: SocketAddr = format!("0.0.0.0:{grpc_port}").parse()?;
    info!(%grpc_addr, "workflow engine listening (gRPC)");

    Server::builder()
        .add_service(WorkflowServiceServer::new(svc))
        .serve_with_shutdown(grpc_addr, wait_for_shutdown(shutdown_rx.clone()))
        .await?;

    // gRPC server has returned — tell the sidecar + timer loop to drain, then join them.
    let _ = shutdown_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }

    Ok(())
}

/// Resolve when the shutdown watch flips to `true`. Shared by the gRPC server and the
/// metrics sidecar as their graceful-shutdown future.
async fn wait_for_shutdown(mut rx: watch::Receiver<bool>) {
    if *rx.borrow() {
        return;
    }
    while rx.changed().await.is_ok() {
        if *rx.borrow() {
            break;
        }
    }
}
