//! V2 — Cache layer (cache-aside + stampede protection).
//!
//! The redirect path is read-heavy and must mostly avoid Postgres. This module
//! wraps a Redis connection manager (cheap to clone, multiplexed) and is where
//! you implement the caching strategy described in SPEC.md.

use redis::aio::ConnectionManager;
use redis::AsyncCommands;

/// What we cache against a slug. `Missing` is the *negative cache* entry: it lets
/// us remember "this slug doesn't exist" so 404 floods don't hit the DB.
#[derive(Debug, Clone)]
pub enum Cached {
    Found { link_id: i64, long_url: String },
    Missing,
}

#[derive(Clone)]
pub struct Cache {
    conn: ConnectionManager,
}

impl Cache {
    pub fn new(conn: ConnectionManager) -> Self {
        Self { conn }
    }

    fn key(slug: &str) -> String {
        format!("link:{slug}")
    }

    /// Look up a slug in the cache.
    ///
    /// TODO(V2): fetch `link:{slug}` from Redis. Decide an on-the-wire encoding
    /// (JSON? a tiny custom string?). Map a sentinel value to `Cached::Missing`
    /// for negative caching. `Ok(None)` means "not in cache at all" (a real miss).
    pub async fn get(&self, slug: &str) -> Result<Option<Cached>, redis::RedisError> {
        let _conn = self.conn.clone();
        let _ = Self::key(slug);
        todo!("V2: read from redis, handle Found / Missing / real-miss")
    }

    /// Store a positive entry with a TTL.
    ///
    /// TODO(V2): SET with an expiry. Pick a TTL (and consider jitter to avoid a
    /// synchronized expiry stampede). Think about probabilistic early expiration.
    pub async fn put_found(
        &self,
        slug: &str,
        link_id: i64,
        long_url: &str,
    ) -> Result<(), redis::RedisError> {
        let mut conn = self.conn.clone();
        let _ = (link_id, long_url);
        // Example shape (replace with your encoding + TTL handling):
        let _: () = conn.set_ex(Self::key(slug), "TODO", 3600).await?;
        todo!("V2: store positive entry properly (encoding + TTL + jitter)")
    }

    /// Store a negative entry (slug known not to exist) with a SHORT TTL.
    /// TODO(V2): implement negative caching.
    pub async fn put_missing(&self, slug: &str) -> Result<(), redis::RedisError> {
        let _ = Self::key(slug);
        todo!("V2: store negative-cache entry with a short TTL")
    }

    /// THE HARD PART — stampede protection.
    ///
    /// When a hot slug expires, thousands of concurrent requests must not all
    /// rebuild it from Postgres at once. TODO(V2): implement one of —
    ///   - single-flight (only one rebuilds, others await the result),
    ///   - a short distributed lock (SET NX PX) with a fallback,
    ///   - probabilistic early recomputation.
    ///
    /// Document your choice in docs/01-design.md.
    pub async fn get_or_rebuild<F, Fut>(
        &self,
        _slug: &str,
        _rebuild: F,
    ) -> Result<Cached, redis::RedisError>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<Cached, redis::RedisError>>,
    {
        todo!("V2: stampede-safe get-or-rebuild")
    }
}
