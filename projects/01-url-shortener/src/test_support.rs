//! Shared fixtures for integration tests (Postgres + scoped Redis + [`AppState`]).
//!
//! Unit tests that need no I/O stay in their module's `#[cfg(test)] mod tests`.
//! Route/cache/auth tests that hit Redis or Postgres should use helpers here.

use std::collections::HashSet;
use std::sync::Arc;

use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use sqlx::postgres::PgPoolOptions;
use sqlx::PgPool;
use tokio::sync::mpsc;

use crate::cache::Cache;
use crate::id_gen::IdGenerator;
use crate::{AppState, ClickEvent};

/// Redis URL for tests. Defaults to logical DB 1 (dev/app uses DB 0 via
/// `REDIS_URL`) so `cargo test` never touches the cache a running dev server
/// uses. Override `TEST_REDIS_URL` to relocate it (e.g. a non-default host/port
/// in CI) — the DB-1 default keeps the isolation guarantee for free.
pub fn test_redis_url() -> String {
    std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/1".into())
}

pub fn unique_slug(prefix: &str) -> String {
    format!(
        "{prefix}-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )
}

/// Real Postgres pool for redirect/create_link integration tests.
pub async fn pg_pool() -> PgPool {
    common_config::load_dotenv();
    let url = std::env::var("DATABASE_URL")
        .unwrap_or_else(|_| "postgres://shortener:shortener@localhost:5432/shortener".into());
    PgPoolOptions::new()
        .max_connections(5)
        .connect(&url)
        .await
        .expect("postgres (is docker compose up? did you run migrations?)")
}

/// Lazy pool for tests that never query Postgres (e.g. auth middleware only).
pub fn lazy_pg_pool() -> PgPool {
    PgPoolOptions::new()
        .connect_lazy("postgres://localhost:5432/unused")
        .expect("lazy pool")
}

/// Isolated Redis: DB 1 + `test:{scope-id}:` key prefix. Call [`Self::cleanup`] after the test.
pub struct RedisTestScope {
    pub cache: Cache,
    pub conn: ConnectionManager,
    key_prefix: String,
    slugs: Vec<String>,
}

impl RedisTestScope {
    pub async fn new() -> Self {
        let scope_id = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();

        let redis_url = test_redis_url();
        let client = redis::Client::open(redis_url).expect("redis client (is docker compose up?)");
        let conn = client
            .get_connection_manager()
            .await
            .expect("redis connection manager when redis is running");

        let key_prefix = format!("test:{scope_id}:");
        let cache = Cache::with_key_prefix(conn.clone(), key_prefix.clone());
        Self {
            cache,
            conn,
            key_prefix,
            slugs: Vec::new(),
        }
    }

    pub fn track(&mut self, slug: &str) {
        self.slugs.push(slug.to_owned());
    }

    pub fn redis_key(&self, slug: &str) -> String {
        format!("{}link:{slug}", self.key_prefix)
    }

    pub async fn cleanup(&mut self) {
        if self.slugs.is_empty() {
            return;
        }
        let keys: Vec<String> = self.slugs.iter().map(|slug| self.redis_key(slug)).collect();
        let mut conn = self.conn.clone();
        let _: redis::RedisResult<()> = conn.del(keys).await;
        self.slugs.clear();
    }
}

/// Build [`AppState`] with a caller-supplied cache (use [`RedisTestScope::cache`] in integration tests).
pub async fn app_state(cache: Cache, api_keys: &[&str]) -> AppState {
    app_state_with_db(cache, api_keys, pg_pool().await).await
}

/// Like [`app_state`] but accepts an existing pool (e.g. lazy pool for auth-only tests).
pub async fn app_state_with_db(cache: Cache, api_keys: &[&str], db: PgPool) -> AppState {
    let (clicks_tx, _clicks_rx) = mpsc::channel::<ClickEvent>(100);
    AppState {
        db,
        cache,
        ids: Arc::new(IdGenerator::new(0)),
        clicks: clicks_tx,
        api_keys: Arc::new(
            api_keys
                .iter()
                .map(|s| (*s).to_string())
                .collect::<HashSet<_>>(),
        ),
        limiter: crate::ratelimit::RateLimiter::new(),
        base_url: Arc::from("http://localhost:8080"),
    }
}

/// Postgres + scoped Redis + [`AppState`] for full-stack route tests (`create_link`, `redirect`, …).
pub struct IntegrationFixtures {
    pub state: AppState,
    pub redis: RedisTestScope,
}

impl IntegrationFixtures {
    pub async fn new() -> Self {
        let redis = RedisTestScope::new().await;
        let state = app_state(redis.cache.clone(), &["dev-secret-key"]).await;
        Self { state, redis }
    }

    pub async fn cleanup(&mut self) {
        self.redis.cleanup().await;
    }
}

#[cfg(test)]
mod smoke_tests {
    use super::*;

    #[tokio::test]
    async fn integration_fixtures_wires_app_state() {
        let mut fx = IntegrationFixtures::new().await;
        assert!(fx.state.api_keys.contains("dev-secret-key"));
        fx.cleanup().await;
    }
}
