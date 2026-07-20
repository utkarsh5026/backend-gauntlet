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

use crate::cache::Cache;
use crate::id_gen::IdGenerator;
use crate::ingest::ClickIngestor;
use crate::AppState;

/// Redis URL for tests. Prefers `TEST_REDIS_URL`, else derives DB 1 from
/// `REDIS_URL` (so CI's `redis://localhost:6379` works), else the local Compose
/// host port (`6301`). Logical DB 1 keeps `cargo test` off the DB 0 cache a
/// running `make run` uses.
pub fn test_redis_url() -> String {
    if let Ok(url) = std::env::var("TEST_REDIS_URL") {
        return url;
    }
    if let Ok(url) = std::env::var("REDIS_URL") {
        return with_redis_db(&url, 1);
    }
    "redis://127.0.0.1:6301/1".into()
}

/// Same Redis host/port as [`test_redis_url`], but logical DB 0 (app namespace).
/// Used by tests that assert scoped keys never land on the production DB.
pub fn app_redis_url() -> String {
    with_redis_db(&test_redis_url(), 0)
}

/// Force logical Redis DB `db` on a URL that may already include a DB index.
fn with_redis_db(url: &str, db: u8) -> String {
    let (base, suffix) = match url.find(['?', '#']) {
        Some(i) => (&url[..i], &url[i..]),
        None => (url, ""),
    };
    let base = base.trim_end_matches('/');
    if let Some(slash) = base.rfind('/') {
        let after = &base[slash + 1..];
        if !after.is_empty() && after.chars().all(|c| c.is_ascii_digit()) {
            return format!("{}{db}{suffix}", &base[..=slash]);
        }
    }
    format!("{base}/{db}{suffix}")
}

fn slug_id() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

pub fn unique_slug(prefix: &str) -> String {
    let id = slug_id();
    format!("{prefix}-{id}")
}

/// Real Postgres pool for redirect/create_link integration tests.
pub async fn pg_pool() -> PgPool {
    common_config::load_dotenv();
    let url = std::env::var("DATABASE_URL")
        .unwrap_or_else(|_| "postgres://shortener:shortener@localhost:5401/shortener".into());
    PgPoolOptions::new()
        .max_connections(5)
        .connect(&url)
        .await
        .expect("postgres (is docker compose up? did you run migrations?)")
}

/// Lazy pool for tests that never query Postgres (e.g. auth middleware only).
pub fn lazy_pg_pool() -> PgPool {
    PgPoolOptions::new()
        .connect_lazy("postgres://localhost:5401/unused")
        .expect("lazy pool")
}

/// A [`Cache`] on the **isolated test DB** for tests that hold an [`AppState`] but
/// never read or write the cache (e.g. auth-middleware tests). Unlike
/// [`RedisTestScope`] it skips key-prefix scoping and cleanup — there are no keys to
/// isolate — but it still honours [`test_redis_url`] (DB 1) so it never touches the
/// cache a running dev server uses on DB 0.
pub async fn test_cache() -> Cache {
    let client =
        redis::Client::open(test_redis_url()).expect("redis client (is docker compose up?)");
    let conn = client
        .get_connection_manager()
        .await
        .expect("redis connection manager when redis is running");
    Cache::new(conn)
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
        let scope_id = slug_id();

        let conn = {
            let redis_url = test_redis_url();
            let client =
                redis::Client::open(redis_url).expect("redis client (is docker compose up?)");
            client
                .get_connection_manager()
                .await
                .expect("redis connection manager when redis is running")
        };

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

/// Drop bomb: deleting test keys needs `async` and `Drop` is sync, so this scope
/// can't clean up after itself — it instead *enforces* that you called
/// [`Self::cleanup`]. Forgetting becomes a loud failure rather than silent cruft
/// accumulating on DB 1.
///
/// The [`std::thread::panicking`] guard is essential: a test that fails leaves
/// tracked slugs behind, and panicking again while that failure is already
/// unwinding would abort the whole test process and bury the real error.
impl Drop for RedisTestScope {
    fn drop(&mut self) {
        if !self.slugs.is_empty() && !std::thread::panicking() {
            panic!(
                "RedisTestScope dropped with {} un-cleaned slug(s) — call `cleanup().await` \
                 before the test ends",
                self.slugs.len()
            );
        }
    }
}

/// Build [`AppState`] with a caller-supplied cache (use [`RedisTestScope::cache`] in integration tests).
pub async fn app_state(cache: Cache, api_keys: &[&str]) -> AppState {
    app_state_with_db(cache, api_keys, pg_pool().await).await
}

/// Like [`app_state`] but accepts an existing pool (e.g. lazy pool for auth-only tests).
pub async fn app_state_with_db(cache: Cache, api_keys: &[&str], db: PgPool) -> AppState {
    // Tests don't care about click delivery: keep the sink, drop the ingestor
    // (a dropped receiver just makes `accept` a silent no-op).
    let (_ingestor, clicks) = ClickIngestor::new(db.clone());
    AppState {
        db,
        cache,
        ids: Arc::new(IdGenerator::new(0)),
        clicks,
        api_keys: Arc::new(
            api_keys
                .iter()
                .map(|s| (*s).to_string())
                .collect::<HashSet<_>>(),
        ),
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

    #[test]
    fn with_redis_db_appends_or_replaces_index() {
        assert_eq!(
            with_redis_db("redis://localhost:6379", 1),
            "redis://localhost:6379/1"
        );
        assert_eq!(
            with_redis_db("redis://localhost:6379/0", 1),
            "redis://localhost:6379/1"
        );
        assert_eq!(
            with_redis_db("redis://127.0.0.1:6301/0?protocol=3", 1),
            "redis://127.0.0.1:6301/1?protocol=3"
        );
    }

    #[tokio::test]
    async fn integration_fixtures_wires_app_state() {
        let mut fx = IntegrationFixtures::new().await;
        assert!(fx.state.api_keys.contains("dev-secret-key"));
        fx.cleanup().await;
    }

    /// Pins the drop-bomb contract: tracking a slug and then dropping the scope
    /// *without* `cleanup().await` must panic, so a future refactor can't silently
    /// disarm it. We track a never-written slug, so nothing actually leaks on DB 1.
    #[tokio::test]
    #[should_panic(expected = "un-cleaned slug")]
    async fn dropping_scope_without_cleanup_panics() {
        let mut scope = RedisTestScope::new().await;
        scope.track(&unique_slug("forgotten"));
        // Intentionally no `scope.cleanup().await` — the `Drop` impl must fire here.
    }
}
