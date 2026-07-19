//! V2 — The request routing engine.
//!
//! Module: `src/router.rs`. Maps an inbound `(host, path, method)` to a route, and
//! thus to an upstream pool. The scaffold builds the table from config (plumbing);
//! `match_request` is the `todo!()`. The naive linear scan works but is O(routes)
//! and ambiguous on overlapping prefixes — build a structure that resolves
//! **longest-prefix** deterministically and sub-linearly. See SPEC.md V2.

use std::sync::Arc;

use axum::http::Method;

use crate::balancer::{Backend, Balancer};
use crate::config::GatewayConfig;

/// A resolved upstream: a named pool with its balancer (V3).
pub struct Upstream {
    pub name: String,
    pub balancer: Balancer,
}

/// One compiled route: a match rule plus the upstream to forward to.
pub struct Route {
    pub name: String,
    /// Host constraint (`None` = any host).
    pub host: Option<String>,
    /// Longest-prefix match target, e.g. `/api/v2`.
    pub path_prefix: String,
    /// Allowed methods (empty = any).
    pub methods: Vec<Method>,
    pub upstream: Arc<Upstream>,
}

/// The route table.
pub struct Router {
    routes: Vec<Route>,
}

impl Router {
    /// Compile the config into the route table (plumbing). Each route's backends
    /// become a pool behind a `Balancer`; the *matching structure* is yours to build.
    pub fn build(cfg: &GatewayConfig) -> anyhow::Result<Self> {
        let mut routes = Vec::with_capacity(cfg.routes.len());
        for rc in &cfg.routes {
            let backends: Vec<Arc<Backend>> = rc
                .upstream
                .backends
                .iter()
                .map(|b| Backend::new(b))
                .collect();
            if backends.is_empty() {
                anyhow::bail!("route `{}` has no backends", rc.name);
            }
            let methods = rc
                .methods
                .iter()
                .map(|m| {
                    m.parse::<Method>()
                        .map_err(|e| anyhow::anyhow!("route `{}` bad method `{m}`: {e}", rc.name))
                })
                .collect::<anyhow::Result<Vec<_>>>()?;

            let upstream = Arc::new(Upstream {
                name: rc.name.clone(),
                balancer: Balancer::new(rc.upstream.lb, backends),
            });
            routes.push(Route {
                name: rc.name.clone(),
                host: rc.host.clone(),
                path_prefix: rc.path_prefix.clone(),
                methods,
                upstream,
            });
        }
        Ok(Self { routes })
    }

    /// Route names, for `GET /admin/routes`.
    pub fn route_names(&self) -> impl Iterator<Item = &str> {
        self.routes.iter().map(|r| r.name.as_str())
    }

    /// Every backend across every route — used by the active health checker (V4)
    /// to probe the whole fleet.
    pub fn backends(&self) -> impl Iterator<Item = &Arc<Backend>> {
        self.routes
            .iter()
            .flat_map(|r| r.upstream.balancer.backends().iter())
    }

    /// Resolve a request to a route, or `None` (→ 404 no route).
    ///
    /// TODO(V2): match on `host` + longest `path_prefix` + `method`. Longest-prefix
    /// must win deterministically (not by insertion order), host/method constraints
    /// must be honoured, and it must stay sub-linear as the table grows — build a
    /// prefix tree / sorted structure rather than scanning `self.routes` every call.
    pub fn match_request(&self, host: Option<&str>, path: &str, method: &Method) -> Option<&Route> {
        let _ = (&self.routes, host, path, method);
        todo!("V2: match (host, path prefix, method) → route; longest-prefix wins")
    }
}
