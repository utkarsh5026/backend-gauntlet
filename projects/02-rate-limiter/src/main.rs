//! Distributed rate limiter — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the Redis connection, the gRPC server,
//! graceful shutdown) is wired up for you. The learning lives in the modules
//! marked `TODO(Vx)`: the token bucket (V1), the sliding window (V2), and the
//! atomic Redis+Lua limiter the server actually calls (V3). See SPEC.md.

mod error;
mod limiter;
mod redis_limiter;
mod sliding_window;
mod token_bucket;

use std::net::SocketAddr;

use tonic::transport::Server;
use tonic::{Request, Response, Status};
use tracing::info;

use limiter::{Algorithm, Decision, LimitConfig};
use redis_limiter::RedisLimiter;

pub mod pb {
    tonic::include_proto!("ratelimit.v1");
}

use pb::rate_limiter_server::{RateLimiter, RateLimiterServer};
use pb::{CheckRequest, CheckResponse, PeekRequest};

const DEFAULT_PORT: u16 = 50051;

/// The gRPC service: a thin adapter from protobuf messages to the limiter and
/// back. All the interesting logic is behind `limiter`.
pub struct RateLimiterSvc {
    limiter: RedisLimiter,
}

#[tonic::async_trait]
impl RateLimiter for RateLimiterSvc {
    async fn check(
        &self,
        request: Request<CheckRequest>,
    ) -> Result<Response<CheckResponse>, Status> {
        let req = request.into_inner();
        let cost = u64::from(req.cost).max(1);
        let decision = self.limiter.check(&req.key, cost).await?;
        Ok(Response::new(to_response(decision)))
    }

    async fn peek(&self, request: Request<PeekRequest>) -> Result<Response<CheckResponse>, Status> {
        let req = request.into_inner();
        let decision = self.limiter.peek(&req.key).await?;
        Ok(Response::new(to_response(decision)))
    }
}

/// Map an internal [`Decision`] onto the wire response.
fn to_response(d: Decision) -> CheckResponse {
    CheckResponse {
        allowed: d.allowed,
        remaining: d.remaining,
        limit: d.limit,
        retry_after_ms: d.retry_after.as_millis() as u64,
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,rate_limiter=debug");

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let redis_url = common_config::require("REDIS_URL")?;
    let cfg = LimitConfig {
        rate_per_sec: common_config::parse_or("RATE_PER_SEC", 10.0),
        burst: common_config::parse_or("BURST", 20),
    };
    let algorithm: Algorithm = common_config::parse_or("ALGORITHM", Algorithm::TokenBucket);
    let fail_open: bool = common_config::parse_or("FAIL_OPEN", true);

    let redis_client = redis::Client::open(redis_url)?;
    let conn_manager = redis_client.get_connection_manager().await?;
    info!("connected to redis");

    let limiter = RedisLimiter::new(conn_manager, cfg, algorithm, fail_open);
    let svc = RateLimiterSvc { limiter };

    let addr: SocketAddr = format!("0.0.0.0:{port}").parse()?;
    info!(%addr, ?algorithm, fail_open, "rate limiter listening (gRPC)");

    Server::builder()
        .add_service(RateLimiterServer::new(svc))
        .serve_with_shutdown(addr, shutdown_signal())
        .await?;

    Ok(())
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
