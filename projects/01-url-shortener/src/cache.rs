//! V2 — Cache layer (cache-aside + stampede protection).
//!
//! The redirect path is read-heavy and must mostly avoid Postgres. This module
//! wraps a Redis connection manager (cheap to clone, multiplexed) and is where
//! you implement the caching strategy described in SPEC.md.

use std::collections::HashMap;
use std::future::Future;
use std::sync::Arc;

use redis::AsyncCommands;
use redis::{aio::ConnectionManager, RedisError};
use serde::{Deserialize, Serialize};
use tokio::sync::{Mutex, Notify};

type RedisResult<T> = Result<T, redis::RedisError>;

/// What we cache against a slug. `Missing` is the *negative cache* entry: it lets
/// us remember "this slug doesn't exist" so 404 floods don't hit the DB.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Cached {
    Found { link_id: i64, long_url: String },
    Missing,
}

/// In-process single-flight slot: one task rebuilds, concurrent waiters block until
/// `result` is published.
#[derive(Default)]
struct InflightRebuild {
    notify: Notify,
    result: Mutex<Option<Result<Cached, String>>>,
}

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

    fn found_ttl_secs() -> u64 {
        let jitter = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.subsec_nanos() as u64)
            .unwrap_or(0)
            % Self::FOUND_TTL_JITTER_SECS;
        Self::FOUND_TTL_SECS + jitter
    }

    /// Look up a slug in the cache.
    ///
    /// `Ok(None)` means the key is absent (real miss — caller should consult Postgres).
    pub async fn get(&self, slug: &str) -> RedisResult<Option<Cached>> {
        let mut conn = self.conn.clone();
        let value: Option<String> = conn.get(self.key(slug)).await?;
        value.map(|v| Self::decode(&v)).transpose()
    }

    /// Store a positive entry with a TTL (+ jitter).
    pub async fn put_found(&self, slug: &str, link_id: i64, long_url: &str) -> RedisResult<()> {
        let mut conn = self.conn.clone();
        let payload = Cached::Found {
            link_id,
            long_url: long_url.to_string(),
        };
        let payload = Self::encode(&payload)?;
        let _: () = conn
            .set_ex(self.key(slug), payload, Self::found_ttl_secs())
            .await?;
        Ok(())
    }

    /// Store a negative entry (slug known not to exist) with a short TTL.
    pub async fn put_missing(&self, slug: &str) -> RedisResult<()> {
        let mut conn = self.conn.clone();
        let payload = Self::encode(&Cached::Missing)?;
        let _: () = conn
            .set_ex(self.key(slug), payload, Self::MISSING_TTL_SECS)
            .await?;
        Ok(())
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
    pub async fn get_or_rebuild<F, Fut>(&self, slug: &str, rebuild: F) -> RedisResult<Cached>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = RedisResult<Cached>>,
    {
        if let Some(cached) = self.get(slug).await? {
            return Ok(cached);
        }

        let is_leader = {
            let mut inflight = self.inflight.lock().await;
            if inflight.contains_key(slug) {
                false
            } else {
                inflight.insert(slug.to_owned(), Arc::new(InflightRebuild::default()));
                true
            }
        };

        if !is_leader {
            let inflight = self.inflight.lock().await;
            let entry = inflight.get(slug).unwrap().clone();
            loop {
                if let Some(outcome) = entry.result.lock().await.clone() {
                    return outcome.map_err(rebuild_error);
                }
                entry.notify.notified().await;
            }
        }

        let inflight = self.inflight.lock().await;
        let entry = inflight.get(slug).unwrap().clone();
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

fn rebuild_error(message: String) -> RedisError {
    RedisError::from((redis::ErrorKind::IoError, "cache rebuild", message))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::{unique_slug, RedisTestScope};

    #[test]
    fn found_round_trips_through_json() {
        let original = Cached::Found {
            link_id: 42,
            long_url: "https://example.com".into(),
        };
        let json = Cache::encode(&original).unwrap();
        assert_eq!(Cache::decode(&json).unwrap(), original);
    }

    #[test]
    fn missing_round_trips_through_json() {
        let original = Cached::Missing;
        let json = Cache::encode(&original).unwrap();
        assert_eq!(json, "\"Missing\"");
        assert_eq!(Cache::decode(&json).unwrap(), original);
    }

    #[test]
    fn decode_rejects_invalid_json() {
        let err = Cache::decode("not-json").unwrap_err();
        assert_eq!(err.kind(), redis::ErrorKind::TypeError);
    }

    #[test]
    fn found_ttl_includes_jitter() {
        let ttl = Cache::found_ttl_secs();
        assert!(ttl >= Cache::FOUND_TTL_SECS);
        assert!(ttl <= Cache::FOUND_TTL_SECS + Cache::FOUND_TTL_JITTER_SECS);
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
            Some(Cached::Found {
                link_id: 99,
                long_url: "https://example.com/page".into(),
            })
        );
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn put_missing_then_get_returns_missing() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("missing");
        scope.cache.put_missing(&slug).await.unwrap();
        scope.track(&slug);
        assert_eq!(scope.cache.get(&slug).await.unwrap(), Some(Cached::Missing));
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn missing_and_absent_are_different() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("negative");
        scope.cache.put_missing(&slug).await.unwrap();
        scope.track(&slug);
        assert_eq!(scope.cache.get(&slug).await.unwrap(), Some(Cached::Missing));
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
        assert_eq!(scope.cache.get(&slug).await.unwrap(), Some(Cached::Missing));
        scope
            .cache
            .put_found(&slug, 7, "https://example.com/new")
            .await
            .unwrap();
        scope.track(&slug);
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            Some(Cached::Found {
                link_id: 7,
                long_url: "https://example.com/new".into(),
            })
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
        let mut prod_conn = redis::Client::open("redis://127.0.0.1:6379/0")
            .unwrap()
            .get_connection_manager()
            .await
            .unwrap();
        let prod_hit: Option<String> = prod_conn.get(format!("link:{slug}")).await.unwrap();
        assert!(prod_hit.is_none());
        scope.cleanup().await;
    }
}

/// V2 stampede protection — `Cache::get_or_rebuild`.
///
/// The rebuild closure carries an `AtomicUsize` so each test asserts *how many
/// times* Postgres would have been consulted — that count is the whole point of
/// single-flight, so it's what we pin.
#[cfg(test)]
mod rebuild_tests {
    use super::*;
    use crate::test_support::{unique_slug, RedisTestScope};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use tokio::sync::Barrier;
    use tokio::time::{sleep, Duration};

    async fn setup() -> (RedisTestScope, String, Arc<AtomicUsize>) {
        let scope = RedisTestScope::new().await;
        let slug = unique_slug("warm");
        let count = Arc::new(AtomicUsize::new(0));
        (scope, slug, count)
    }

    #[tokio::test]
    async fn get_or_rebuild_serves_cache_hit_without_rebuilding() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("warm");
        scope
            .cache
            .put_found(&slug, 5, "https://example.com/cached")
            .await
            .unwrap();
        scope.track(&slug);

        let calls = Arc::new(AtomicUsize::new(0));
        let c = calls.clone();
        let cached = scope
            .cache
            .get_or_rebuild(&slug, || async move {
                c.fetch_add(1, Ordering::SeqCst);
                Ok(Cached::Missing)
            })
            .await
            .unwrap();

        assert_eq!(
            cached,
            Cached::Found {
                link_id: 5,
                long_url: "https://example.com/cached".into(),
            }
        );
        assert_eq!(
            calls.load(Ordering::SeqCst),
            0,
            "a cache hit must never invoke the rebuild fn"
        );
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn get_or_rebuild_populates_cache_on_miss() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("cold");
        let calls = Arc::new(AtomicUsize::new(0));
        let c = calls.clone();

        let built = scope
            .cache
            .get_or_rebuild(&slug, || async move {
                c.fetch_add(1, Ordering::SeqCst);
                Ok(Cached::Found {
                    link_id: 9,
                    long_url: "https://example.com/built".into(),
                })
            })
            .await
            .unwrap();
        scope.track(&slug);

        let expected = Cached::Found {
            link_id: 9,
            long_url: "https://example.com/built".into(),
        };
        assert_eq!(built, expected);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        // Proof it was written through: a plain read now hits Redis directly.
        assert_eq!(scope.cache.get(&slug).await.unwrap(), Some(expected));
        scope.cleanup().await;
    }

    #[tokio::test]
    async fn get_or_rebuild_caches_negative_result() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("cold-missing");
        let calls = Arc::new(AtomicUsize::new(0));
        let c = calls.clone();

        let built = scope
            .cache
            .get_or_rebuild(&slug, || async move {
                c.fetch_add(1, Ordering::SeqCst);
                Ok(Cached::Missing)
            })
            .await
            .unwrap();
        scope.track(&slug);

        assert_eq!(built, Cached::Missing);
        assert_eq!(scope.cache.get(&slug).await.unwrap(), Some(Cached::Missing));
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        scope.cleanup().await;
    }

    /// THE headline test: N concurrent misses for one hot slug must trigger exactly
    /// one rebuild, and every caller gets that single result.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn get_or_rebuild_coalesces_concurrent_misses() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("stampede");
        let calls = Arc::new(AtomicUsize::new(0));

        const WAITERS: usize = 32;
        let barrier = Arc::new(Barrier::new(WAITERS));
        let mut handles = Vec::with_capacity(WAITERS);

        for _ in 0..WAITERS {
            let cache = scope.cache.clone();
            let slug = slug.clone();
            let calls = calls.clone();
            let barrier = barrier.clone();
            handles.push(tokio::spawn(async move {
                // Release all tasks together so they truly race the empty cache.
                barrier.wait().await;
                cache
                    .get_or_rebuild(&slug, || async move {
                        calls.fetch_add(1, Ordering::SeqCst);
                        // Hold the in-flight slot open long enough that every waiter
                        // registers behind the leader instead of starting its own.
                        sleep(Duration::from_millis(150)).await;
                        Ok(Cached::Found {
                            link_id: 7,
                            long_url: "https://example.com/hot".into(),
                        })
                    })
                    .await
            }));
        }

        let expected = Cached::Found {
            link_id: 7,
            long_url: "https://example.com/hot".into(),
        };
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
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("stampede-fail");
        let calls = Arc::new(AtomicUsize::new(0));

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
                    .get_or_rebuild(&slug, || async move {
                        calls.fetch_add(1, Ordering::SeqCst);
                        sleep(Duration::from_millis(150)).await;
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

    /// A failed rebuild must not poison the slug: the next request becomes a fresh
    /// leader and succeeds (the in-process slot is torn down after the failure).
    #[tokio::test]
    async fn get_or_rebuild_retries_after_a_failed_rebuild() {
        let mut scope = RedisTestScope::new().await;
        let slug = unique_slug("self-heal");
        let calls = Arc::new(AtomicUsize::new(0));

        let c1 = calls.clone();
        let first = scope
            .cache
            .get_or_rebuild(&slug, || async move {
                c1.fetch_add(1, Ordering::SeqCst);
                Err(RedisError::from((redis::ErrorKind::IoError, "boom")))
            })
            .await;
        assert!(first.is_err());
        assert_eq!(
            scope.cache.get(&slug).await.unwrap(),
            None,
            "the failed attempt left nothing behind"
        );

        let c2 = calls.clone();
        let expected = Cached::Found {
            link_id: 1,
            long_url: "https://example.com".into(),
        };
        let second = scope
            .cache
            .get_or_rebuild(&slug, {
                let expected = expected.clone();
                || async move {
                    c2.fetch_add(1, Ordering::SeqCst);
                    Ok(expected)
                }
            })
            .await
            .unwrap();
        scope.track(&slug);

        assert_eq!(second, expected);
        assert_eq!(
            calls.load(Ordering::SeqCst),
            2,
            "the earlier failure must not block the retry"
        );
        scope.cleanup().await;
    }
}
