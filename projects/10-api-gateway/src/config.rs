//! The gateway's routing configuration — the route → upstream table.
//!
//! This is *plumbing*, so the loader is implemented: routes come from a JSON file
//! (`CONFIG_PATH`) or a built-in catch-all (`demo`). The interesting parts are the
//! router (V2), balancer (V3), and health/circuit breaker (V4) that *consume* this
//! table — those live in their own modules.

use serde::Deserialize;

use crate::balancer::LbPolicy;

/// The whole gateway config: an ordered list of routes.
#[derive(Debug, Clone, Deserialize)]
pub struct GatewayConfig {
    pub routes: Vec<RouteConfig>,
}

/// One route: a match rule (`host` + `path_prefix` + `methods`) and the upstream
/// pool to forward matching requests to.
#[derive(Debug, Clone, Deserialize)]
pub struct RouteConfig {
    /// Human name, used in logs / `GET /admin/routes`.
    pub name: String,
    /// Optional host constraint (`Host:` header must equal this). `None` = any host.
    #[serde(default)]
    pub host: Option<String>,
    /// Longest-prefix match target, e.g. `/api/v2`.
    pub path_prefix: String,
    /// Allowed methods (`["GET","POST"]`). Empty = any method.
    #[serde(default)]
    pub methods: Vec<String>,
    /// The pool of backends and how to balance across them.
    pub upstream: UpstreamConfig,
}

/// A pool of backends plus the load-balancing policy over them.
#[derive(Debug, Clone, Deserialize)]
pub struct UpstreamConfig {
    /// `host:port` of each backend in the pool.
    pub backends: Vec<String>,
    /// Load-balancing policy (defaults to round-robin).
    #[serde(default)]
    pub lb: LbPolicy,
}

impl GatewayConfig {
    /// Load a JSON gateway config from disk. See `gateway.example.json`.
    pub fn load(path: &str) -> anyhow::Result<Self> {
        let raw = std::fs::read_to_string(path)
            .map_err(|e| anyhow::anyhow!("reading gateway config `{path}`: {e}"))?;
        let cfg: GatewayConfig = serde_json::from_str(&raw)
            .map_err(|e| anyhow::anyhow!("parsing gateway config `{path}`: {e}"))?;
        Ok(cfg)
    }

    /// A built-in single catch-all route (`/` → `backends`), used when no
    /// `CONFIG_PATH` is set so `cargo run` and docker-compose need zero files.
    pub fn demo(backends: Vec<String>) -> Self {
        Self {
            routes: vec![RouteConfig {
                name: "default".to_string(),
                host: None,
                path_prefix: "/".to_string(),
                methods: Vec::new(),
                upstream: UpstreamConfig {
                    backends,
                    lb: LbPolicy::default(),
                },
            }],
        }
    }
}
