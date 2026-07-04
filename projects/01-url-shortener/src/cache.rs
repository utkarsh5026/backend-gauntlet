//! V2 — Cache layer (cache-aside + stampede protection).
//!
//! The redirect path is read-heavy and must mostly avoid Postgres. This module
//! wraps a Redis connection manager (cheap to clone, multiplexed) and is where
//! you implement the caching strategy described in SPEC.md.

use std::collections::HashMap;
use std::future::Future;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use redis::AsyncCommands;
use redis::{aio::ConnectionManager, RedisError};
use serde::{Deserialize, Serialize};
use tokio::sync::{Mutex, Notify};

type RedisResult<T> = Result<T, redis::RedisError>;

/// What we cache against a slug. `Missing` is the *negative cache* entry: it lets
/// us remember "this slug doesn't exist" so 404 floods don't hit the DB.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Cached {
    /// A positive hit: the slug resolves to a live link.
    Found {
        /// Primary key of the link row in Postgres.
        link_id: i64,
        /// Destination the slug redirects to.
        long_url: String,
    },
    /// The slug is known not to exist (negative cache entry).
    Missing,
}

#[cfg(test)]
impl Cached {
    fn found(link_id: i64, long_url: impl Into<String>) -> Self {
        Self::Found {
            link_id,
            long_url: long_url.into(),
        }
    }

    fn missing() -> Self {
        Self::Missing
    }
}

/// In-process single-flight slot: one task rebuilds, concurrent waiters block until
/// `result` is published.
#[derive(Default)]
struct InflightRebuild {
    notify: Notify,
    result: Mutex<Option<Result<Cached, String>>>,
}

/// Cache-aside wrapper over a Redis [`ConnectionManager`], plus in-process
/// single-flight state for stampede protection.
///
/// Cheap to [`Clone`]: the connection manager is multiplexed and the in-flight
/// map is shared behind an [`Arc`], so every clone coordinates through the same
/// `InflightRebuild` slots. Positive and negative entries are stored under a
/// prefixed `link:<slug>` key with the TTLs defined by the `*_TTL_SECS` constants.
#[derive(Clone)]
pub struct Cache {
    conn: ConnectionManager,
    key_prefix: String,
    inflight: Arc<Mutex<HashMap<String, Arc<InflightRebuild>>>>,
}

impl Cache {
    const FOUND_TTL_SECS: u64 = 3600;
    const MISSING_TTL_SECS: u64 = 60;
    const FOUND_TTL_JITTER_SECS: u64 = 300;

    /// Build a cache over `conn` with no key prefix (production namespace).
    ///
    /// Use [`Self::with_key_prefix`] to isolate keys, e.g. per test.
    pub fn new(conn: ConnectionManager) -> Self {
        Self {
            conn,
            key_prefix: String::new(),
            inflight: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Same as [`Self::new`] but scopes all keys under `prefix` (must end with `:` if nested).
    pub fn with_key_prefix(conn: ConnectionManager, prefix: impl Into<String>) -> Self {
        Self {
            conn,
            key_prefix: prefix.into(),
            inflight: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    fn key(&self, slug: &str) -> String {
        format!("{}link:{slug}", self.key_prefix)
    }

    fn encode(value: &Cached) -> RedisResult<String> {
        serde_json::to_string(value).map_err(|e| {
            RedisError::from((redis::ErrorKind::TypeError, "cache encode", e.to_string()))
        })
    }

    fn decode(payload: &str) -> RedisResult<Cached> {
        serde_json::from_str(payload).map_err(|e| {
            RedisError::from((
                redis::ErrorKind::TypeError,
                "invalid cache payload",
                e.to_string(),
            ))
        })
    }

    /// Look up a slug in the cache.
    ///
    /// `Ok(None)` means the key is absent (real miss — caller should consult Postgres).
    ///
    /// # Errors
    ///
    /// Returns a [`RedisError`] if the Redis command fails or the stored payload
    /// cannot be decoded into a [`Cached`].
    pub async fn get(&self, slug: &str) -> RedisResult<Option<Cached>> {
        let mut conn = self.conn.clone();
        let value: Option<String> = conn.get(self.key(slug)).await?;
        value.map(|v| Self::decode(&v)).transpose()
    }

    /// Store a positive entry with a TTL (+ jitter).
    ///
    /// Up to [`Self::FOUND_TTL_JITTER_SECS`] of jitter on the base TTL spreads
    /// expiries so hot keys don't all lapse on the same tick and cause a
    /// synchronized stampede.
    ///
    /// # Errors
    ///
    /// Returns a [`RedisError`] if encoding fails or the `SETEX` command fails.
    pub async fn put_found(&self, slug: &str, link_id: i64, long_url: &str) -> RedisResult<()> {
        let mut conn = self.conn.clone();

        let payload = {
            let payload = Cached::Found {
                link_id,
                long_url: long_url.to_string(),
            };
            Self::encode(&payload)?
        };

        let ttl = {
            let jitter_secs = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|t| t.subsec_nanos() as u64 % Self::FOUND_TTL_JITTER_SECS)
                .unwrap_or(0);
            Self::FOUND_TTL_SECS + jitter_secs
        };
        let _: () = conn.set_ex(self.key(slug), payload, ttl).await?;
        Ok(())
    }

    /// Store a negative entry (slug known not to exist) with a short TTL.
    ///
    /// The TTL is deliberately short (`MISSING_TTL_SECS`) so a slug that is later
    /// created becomes reachable soon after.
    ///
    /// # Errors
    ///
    /// Returns a [`RedisError`] if encoding fails or the `SETEX` command fails.
    pub async fn put_missing(&self, slug: &str) -> RedisResult<()> {
        let mut conn = self.conn.clone();
        let payload = Self::encode(&Cached::Missing)?;
        let _: () = conn
            .set_ex(self.key(slug), payload, Self::MISSING_TTL_SECS)
            .await?;
        Ok(())
    }

    /// Read `slug`, rebuilding through `rebuild` on a miss — with stampede
    /// protection (V2).
    ///
    /// When a hot slug expires, thousands of concurrent requests must not all
    /// rebuild it from Postgres at once. This uses **in-process single-flight**:
    /// the first caller to miss becomes the leader and runs `rebuild`; every
    /// other caller for the same slug parks on the leader's `InflightRebuild`
    /// slot and receives the leader's result — so Postgres is consulted once per
    /// herd, not once per request. A cache hit skips `rebuild` entirely.
    ///
    /// On success the value is written through ([`Self::put_found`] /
    /// [`Self::put_missing`]) before waiters are woken. A failed rebuild is
    /// *not* cached and the slot is torn down, so the next request becomes a
    /// fresh leader and retries. See `docs/01-design.md` for the design rationale.
    ///
    /// # Errors
    ///
    /// Returns a [`RedisError`] if the initial lookup fails, if writing the
    /// rebuilt value fails, or if `rebuild` itself errors (the error is
    /// propagated to every parked waiter as a [`redis::ErrorKind::IoError`]).
    pub async fn get_or_rebuild<F, Fut>(&self, slug: &str, rebuild: F) -> RedisResult<Cached>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = RedisResult<Cached>>,
    {
        if let Some(cached) = self.get(slug).await? {
            return Ok(cached);
        }

        // Elect a leader and grab the shared slot in one critical section. Cloning
        // the `Arc` here — instead of re-locking the map to fetch it later — means
        // the leader can't `remove` the slot in the gap, so a waiter always holds a
        // live slot even after teardown. (Fetching it under a *second* lock is the
        // race that panicked `get(slug).unwrap()` on an already-removed slot.)
        let (entry, is_leader) = {
            let mut inflight = self.inflight.lock().await;
            match inflight.get(slug) {
                Some(entry) => (entry.clone(), false),
                None => {
                    let entry = Arc::new(InflightRebuild::default());
                    inflight.insert(slug.to_owned(), entry.clone());
                    (entry, true)
                }
            }
        };

        if !is_leader {
            // Register for the wakeup *before* reading the result: `notify_waiters`
            // only wakes tasks already parked, so checking-then-parking could miss
            // the leader's notify and hang forever. `enable()` queues us first.
            loop {
                let notified = entry.notify.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                if let Some(outcome) = entry.result.lock().await.clone() {
                    return outcome.map_err(|e| {
                        RedisError::from((redis::ErrorKind::IoError, "cache rebuild", e))
                    });
                }
                notified.await;
            }
        }

        let outcome = rebuild().await.map_err(|e| e.to_string());
        if let Ok(ref value) = outcome {
            match value {
                Cached::Found { link_id, long_url } => {
                    self.put_found(slug, *link_id, long_url).await?;
                }
                Cached::Missing => {
                    self.put_missing(slug).await?;
                }
            }
        }

        *entry.result.lock().await = Some(outcome.clone());
        entry.notify.notify_waiters();
        self.inflight.lock().await.remove(slug);

        outcome.map_err(|e| RedisError::from((redis::ErrorKind::IoError, "cache rebuild", e)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::{unique_slug, RedisTestScope};

    #[test]
    fn found_round_trips_through_json() {
        let original = Cached::found(42, "https://example.com");
        let json = Cache::encode(&original).unwrap();
        assert_eq!(Cache::decode(&json).unwrap(), original);
    }

    #[test]
    fn missing_round_trips_through_json() {
        let original = Cached::missing();
        let json = Cache::encode(&original).unwrap();
        assert_eq!(json, "\"Missing\"");
        assert_eq!(Cache::decode(&json).unwrap(), original);
    }

    #[test]
    fn decode_rejects_invalid_json() {
        let err = Cache::decode("not-json").unwrap_err();
        assert_eq!(err.kind(), redis::ErrorKind::TypeError);
    }

    #[tokio::test]
    async fn get_returns_none_for_unknown_slug() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("absent");
        assert_eq!(scope.cache.get(&slug).await.unwrap(), None);
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn put_found_then_get_returns_found() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("found");
        scope
            .cache
            .put_found(&slug, 99, "https://example.com/page")
            .await
            .unwrap();
        scope.track(&slug);
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            Some(Cached::found(99, "https://example.com/page"))
        );
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn put_missing_then_get_returns_missing() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("missing");
        scope.cache.put_missing(&slug).await.unwrap();
        scope.track(&slug);
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            Some(Cached::missing())
        );
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn missing_and_absent_are_different() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("negative");
        scope.cache.put_missing(&slug).await.unwrap();
        scope.track(&slug);
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            Some(Cached::missing())
        );
        assert_eq!(
            scope
                .cache
                .get(&unique_slug("never-written"))
                .await
                .unwrap(),
            None
        );
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn put_found_overwrites_missing() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("flip");
        scope.cache.put_missing(&slug).await.unwrap();
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            Some(Cached::missing())
        );
        scope
            .cache
            .put_found(&slug, 7, "https://example.com/new")
            .await
            .unwrap();
        scope.track(&slug);
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            Some(Cached::found(7, "https://example.com/new"))
        );
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn put_missing_sets_short_ttl() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("ttl-missing");
        scope.cache.put_missing(&slug).await.unwrap();
        scope.track(&slug);
        let mut conn = scope.conn.clone();
        let ttl: i64 = conn.ttl(scope.redis_key(&slug)).await.unwrap();
        assert!((1..=Cache::MISSING_TTL_SECS as i64).contains(&ttl));
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn put_found_sets_longer_ttl_than_missing() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("ttl-found");
        scope
            .cache
            .put_found(&slug, 1, "https://example.com")
            .await
            .unwrap();
        scope.track(&slug);
        let mut conn = scope.conn.clone();
        let ttl: i64 = conn.ttl(scope.redis_key(&slug)).await.unwrap();
        assert!(ttl > Cache::MISSING_TTL_SECS as i64);
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn get_rejects_corrupt_redis_payload() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("corrupt");
        let mut conn = scope.conn.clone();
        let _: () = conn
            .set_ex(scope.redis_key(&slug), "{bad", 60)
            .await
            .unwrap();
        scope.track(&slug);
        let err = scope.cache.get(&slug).await.unwrap_err();
        assert_eq!(err.kind(), redis::ErrorKind::TypeError);
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn scoped_keys_do_not_use_production_namespace() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("scoped");
        scope
            .cache
            .put_found(&slug, 1, "https://example.com")
            .await
            .unwrap();
        scope.track(&slug);
        let mut prod_conn = redis::Client::open("redis://127.0.0.1:6301/0")
            .unwrap()
            .get_connection_manager()
            .await
            .unwrap();
        let prod_hit: Option<String> = prod_conn.get(format!("link:{slug}")).await.unwrap();
        assert!(prod_hit.is_none());
        scope.cleanup().await;
    }
}

#[cfg(test)]
mod rebuild_tests {
    use super::*;
    use crate::test_support::{unique_slug, RedisTestScope};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use tokio::sync::Barrier;
    use tokio::time::Duration;

    async fn setup(prefix: &'static str) -> (RedisTestScope, String, Arc<AtomicUsize>) {
        let scope = RedisTestScope::new().await;
        let slug = unique_slug(prefix);
        let count = Arc::new(AtomicUsize::new(0));
        (scope, slug, count)
    }

    async fn counted_get_or_rebuild<F, Fut>(
        scope: &RedisTestScope,
        slug: &str,
        count: &Arc<AtomicUsize>,
        rebuild: F,
    ) -> RedisResult<Cached>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = RedisResult<Cached>>,
    {
        let count = Arc::clone(count);
        scope
            .cache
            .get_or_rebuild(slug, move || async move {
                count.fetch_add(1, Ordering::SeqCst);
                rebuild().await
            })
            .await
    }

    #[tokio::test]
    async fn get_or_rebuild_serves_cache_hit_without_rebuilding() {
        let (mut scope, slug, count) = setup("warm").await;
        scope
            .cache
            .put_found(&slug, 5, "https://example.com/cached")
            .await
            .unwrap();
        scope.track(&slug);

        let cached =
            counted_get_or_rebuild(&scope, &slug, &count, || async { Ok(Cached::missing()) }).await;

        assert_eq!(
            cached.unwrap(),
            Cached::found(5, "https://example.com/cached")
        );
        assert_eq!(
            count.load(Ordering::SeqCst),
            0,
            "a cache hit must never invoke the rebuild fn"
        );
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn get_or_rebuild_populates_cache_on_miss() {
        let (mut scope, slug, count) = setup("cold").await;
        let built = counted_get_or_rebuild(&scope, &slug, &count, || async {
            Ok(Cached::found(9, "https://example.com/built"))
        })
        .await;
        scope.track(&slug);

        let expected = Cached::found(9, "https://example.com/built");
        assert_eq!(built.unwrap(), expected);
        assert_eq!(count.load(Ordering::SeqCst), 1);
        // Proof it was written through: a plain read now hits Redis directly.
        assert_eq!(scope.cache.get(&slug).await.unwrap(), Some(expected));
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn get_or_rebuild_caches_negative_result() {
        let (mut scope, slug, count) = setup("cold-missing").await;
        let built =
            counted_get_or_rebuild(&scope, &slug, &count, || async { Ok(Cached::missing()) }).await;
        scope.track(&slug);

        assert_eq!(built.unwrap(), Cached::missing());
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            Some(Cached::missing())
        );
        assert_eq!(count.load(Ordering::SeqCst), 1);
        scope.cleanup().await;
    }

    /// THE headline test: N concurrent misses for one hot slug must trigger exactly
    /// one rebuild, and every caller gets that single result.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn get_or_rebuild_coalesces_concurrent_misses() {
        let (mut scope, slug, calls) = setup("stampede").await;

        const WAITERS: usize = 1_000;
        let barrier = Arc::new(Barrier::new(WAITERS));
        let mut handles = Vec::with_capacity(WAITERS);

        for _ in 0..WAITERS {
            let cache = scope.cache.clone();
            let slug = slug.clone();
            let calls = calls.clone();
            let barrier = barrier.clone();
            handles.push(tokio::spawn(async move {
                barrier.wait().await;
                cache
                    .get_or_rebuild(&slug, || async move {
                        calls.fetch_add(1, Ordering::SeqCst);
                        tokio::time::sleep(Duration::from_millis(150)).await;
                        Ok(Cached::found(7, "https://example.com/hot"))
                    })
                    .await
            }));
        }

        let expected = Cached::found(7, "https://example.com/hot");
        for handle in handles {
            assert_eq!(handle.await.unwrap().unwrap(), expected);
        }
        assert_eq!(
            calls.load(Ordering::SeqCst),
            1,
            "the whole herd must be served by a single rebuild"
        );

        scope.track(&slug);
        scope.cleanup().await;
    }

    /// A failed leader speaks for the herd too: every parked waiter sees the error,
    /// and Postgres is hit once — not once per waiter.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn get_or_rebuild_coalesces_failure_across_waiters() {
        let (mut scope, slug, calls) = setup("stampede-fail").await;

        const WAITERS: usize = 16;
        let barrier = Arc::new(Barrier::new(WAITERS));
        let mut handles = Vec::with_capacity(WAITERS);
        for _ in 0..WAITERS {
            let cache = scope.cache.clone();
            let slug = slug.clone();
            let calls = calls.clone();
            let barrier = barrier.clone();
            handles.push(tokio::spawn(async move {
                barrier.wait().await;
                cache
                    .get_or_rebuild(&slug, move || async move {
                        calls.fetch_add(1, Ordering::SeqCst);
                        tokio::time::sleep(Duration::from_millis(150)).await;
                        Err(RedisError::from((
                            redis::ErrorKind::IoError,
                            "simulated postgres outage",
                        )))
                    })
                    .await
            }));
        }

        for handle in handles {
            assert!(
                handle.await.unwrap().is_err(),
                "every waiter must observe the leader's failure"
            );
        }
        assert_eq!(
            calls.load(Ordering::SeqCst),
            1,
            "one failed attempt, not a retry storm"
        );
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            None,
            "failures must not be cached"
        );
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn get_or_rebuild_retries_after_a_failed_rebuild() {
        let (mut scope, slug, calls) = setup("self-heal").await;
        let first = counted_get_or_rebuild(&scope, &slug, &calls, || async {
            Err(RedisError::from((redis::ErrorKind::IoError, "boom")))
        })
        .await;
        assert!(first.is_err());
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            None,
            "the failed attempt left nothing behind"
        );

        let expected = Cached::found(1, "https://example.com");
        let e = expected.clone();
        let second = counted_get_or_rebuild(&scope, &slug, &calls, || async move { Ok(e) }).await;
        scope.track(&slug);

        assert_eq!(second.unwrap(), expected);
        assert_eq!(
            calls.load(Ordering::SeqCst),
            2,
            "the earlier failure must not block the retry"
        );
        scope.cleanup().await;
    }
}
