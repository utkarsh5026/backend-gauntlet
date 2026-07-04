//! V2 cache-aside slug resolution — the shared read path behind the redirect and
//! the debug inspector. Redis first, Postgres on miss, back-filling positive and
//! negative entries. Extracted from `routes.rs` so the resolution policy (and the
//! "Redis down degrades, not dies" fallback) lives in one place, separate from the
//! HTTP wiring that consumes it.

use crate::cache::Cached;
use crate::error::AppError;
use crate::AppState;

/// Where a slug resolution was ultimately served from — the cache-aside outcome.
/// Exposed to clients as the `X-Cache` header and in the demo's debug JSON.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CacheOutcome {
    /// Served straight from Redis (positive entry).
    Hit,
    /// Redis held a negative entry — short-circuited to 404 without touching Postgres.
    Negative,
    /// Redis missed; Postgres was consulted (and the result back-filled into Redis).
    Miss,
    /// Redis read failed; Postgres was consulted instead (no back-fill attempted).
    Degraded,
}

impl CacheOutcome {
    /// Lowercase label used for the tracing span field (`hit` / `negative` / `miss`).
    pub(crate) fn label(self) -> &'static str {
        match self {
            CacheOutcome::Hit => "hit",
            CacheOutcome::Negative => "negative",
            CacheOutcome::Miss => "miss",
            CacheOutcome::Degraded => "degraded",
        }
    }

    /// Uppercase form for the `X-Cache` response header.
    pub(crate) fn header(self) -> &'static str {
        match self {
            CacheOutcome::Hit => "HIT",
            CacheOutcome::Negative => "NEGATIVE",
            CacheOutcome::Miss => "MISS",
            CacheOutcome::Degraded => "DEGRADED",
        }
    }
}

/// Outcome of resolving a slug through the cache-aside path.
pub(crate) struct Resolved {
    pub(crate) outcome: CacheOutcome,
    pub(crate) link: Option<(i64, String)>,
}

/// Cache-aside resolution shared by the redirect and the debug endpoint: Redis
/// first, Postgres on miss, populating positive/negative entries. This is the V2
/// hot path — kept in one place so the demo's observability sees exactly what a
/// real redirect sees.
///
/// **Degrade, not die (SPEC V2):** a Redis failure on the read must not 500 the
/// redirect — it falls through to Postgres. The cache read is therefore *caught*,
/// not `?`-d, and the back-fill writes are best-effort. Only a *Postgres* failure
/// still propagates.
pub(crate) async fn resolve_slug(state: &AppState, slug: &str) -> Result<Resolved, AppError> {
    let degraded = match state.cache.get(slug).await {
        Ok(Some(Cached::Found { link_id, long_url })) => {
            return Ok(Resolved {
                outcome: CacheOutcome::Hit,
                link: Some((link_id, long_url)),
            });
        }
        Ok(Some(Cached::Missing)) => {
            return Ok(Resolved {
                outcome: CacheOutcome::Negative,
                link: None,
            });
        }
        Ok(None) => false,
        Err(e) => {
            tracing::warn!(%slug, error = %e, "cache read failed; degrading to postgres");
            true
        }
    };

    let row = sqlx::query!("SELECT id, long_url FROM links WHERE slug = $1", slug)
        .fetch_optional(&state.db)
        .await?;

    let link = match row {
        Some(row) => {
            if !degraded {
                let _ = state.cache.put_found(slug, row.id, &row.long_url).await;
            }
            Some((row.id, row.long_url))
        }
        None => {
            if !degraded {
                let _ = state.cache.put_missing(slug).await;
            }
            None
        }
    };

    Ok(Resolved {
        outcome: if degraded {
            CacheOutcome::Degraded
        } else {
            CacheOutcome::Miss
        },
        link,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::Cached;
    use crate::test_support::{app_state_with_db, unique_slug, RedisTestScope};
    use redis::AsyncCommands;
    use sqlx::PgPool;

    #[test]
    fn label_matches_outcome() {
        assert_eq!(CacheOutcome::Hit.label(), "hit");
        assert_eq!(CacheOutcome::Negative.label(), "negative");
        assert_eq!(CacheOutcome::Miss.label(), "miss");
        assert_eq!(CacheOutcome::Degraded.label(), "degraded");
    }

    #[test]
    fn header_is_uppercase_of_label() {
        for outcome in [
            CacheOutcome::Hit,
            CacheOutcome::Negative,
            CacheOutcome::Miss,
            CacheOutcome::Degraded,
        ] {
            assert_eq!(outcome.header(), outcome.label().to_uppercase());
        }
    }

    /// Mirror of `routes.rs::state_and_redis`: an `AppState` sharing a scoped test
    /// cache with the returned `RedisTestScope` so a test can seed/inspect Redis.
    async fn state_and_redis(pool: PgPool) -> (AppState, RedisTestScope) {
        let redis = RedisTestScope::new().await;
        let state = app_state_with_db(redis.cache.clone(), &[], pool).await;
        (state, redis)
    }

    /// A positive cache entry is served straight from Redis — Postgres is never
    /// consulted, proven by the *absence* of any DB row for the slug.
    #[sqlx::test]
    async fn hit_serves_from_cache_without_touching_db(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool).await;
        let slug = unique_slug("hit");
        redis
            .cache
            .put_found(&slug, 7, "https://cached.example.com/x")
            .await
            .unwrap();
        redis.track(&slug);

        let resolved = resolve_slug(&state, &slug).await.unwrap();

        assert_eq!(resolved.outcome, CacheOutcome::Hit);
        assert_eq!(
            resolved.link,
            Some((7, "https://cached.example.com/x".into()))
        );

        redis.cleanup().await;
    }

    /// A negative cache entry short-circuits to "not found" even when the row
    /// exists in Postgres — the whole point of negative caching.
    #[sqlx::test]
    async fn negative_short_circuits_db(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool.clone()).await;
        let slug = unique_slug("neg");

        // Row exists in Postgres...
        sqlx::query!(
            "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)",
            5_i64,
            slug,
            "https://real.example.com",
        )
        .execute(&pool)
        .await
        .unwrap();
        // ...but a cached `Missing` must win.
        redis.cache.put_missing(&slug).await.unwrap();
        redis.track(&slug);

        let resolved = resolve_slug(&state, &slug).await.unwrap();

        assert_eq!(resolved.outcome, CacheOutcome::Negative);
        assert_eq!(resolved.link, None);

        redis.cleanup().await;
    }

    /// A cold cache with a live row resolves via Postgres (Miss) and back-fills a
    /// positive entry so the next read is a Hit.
    #[sqlx::test]
    async fn miss_reads_db_and_backfills_positive(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool.clone()).await;
        let slug = unique_slug("miss");
        sqlx::query!(
            "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)",
            1_i64,
            slug,
            "https://example.com/dest",
        )
        .execute(&pool)
        .await
        .unwrap();

        let resolved = resolve_slug(&state, &slug).await.unwrap();
        redis.track(&slug);

        assert_eq!(resolved.outcome, CacheOutcome::Miss);
        assert_eq!(resolved.link, Some((1, "https://example.com/dest".into())));
        // The miss warmed the cache.
        assert_eq!(
            redis.cache.get(&slug).await.unwrap(),
            Some(Cached::Found {
                link_id: 1,
                long_url: "https://example.com/dest".into(),
            }),
        );

        redis.cleanup().await;
    }

    /// A cold cache with no row resolves via Postgres (Miss) and back-fills a
    /// *negative* entry so a 404 flood can't keep hammering the DB.
    #[sqlx::test]
    async fn miss_on_absent_row_backfills_negative(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool).await;
        let slug = unique_slug("absent");

        let resolved = resolve_slug(&state, &slug).await.unwrap();
        redis.track(&slug);

        assert_eq!(resolved.outcome, CacheOutcome::Miss);
        assert_eq!(resolved.link, None);
        assert_eq!(redis.cache.get(&slug).await.unwrap(), Some(Cached::Missing));

        redis.cleanup().await;
    }

    /// "Degrade, not die" (SPEC V2): a failed cache *read* falls through to
    /// Postgres and, crucially, does **not** back-fill. We drive the same
    /// `Cache::get() -> Err` path as a real outage by planting an undecodable
    /// payload; the observable contract is identical.
    #[sqlx::test]
    async fn degraded_reads_db_and_does_not_backfill(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool.clone()).await;
        let slug = unique_slug("degrade");
        sqlx::query!(
            "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)",
            1_i64,
            slug,
            "https://example.com/survives",
        )
        .execute(&pool)
        .await
        .unwrap();

        // Plant a payload that can't decode into `Cached`, so `cache.get` errors.
        let mut conn = redis.conn.clone();
        let _: () = conn
            .set_ex(redis.redis_key(&slug), "{ not-valid-Cached-json", 60)
            .await
            .unwrap();
        redis.track(&slug);

        let resolved = resolve_slug(&state, &slug).await.unwrap();

        assert_eq!(resolved.outcome, CacheOutcome::Degraded);
        assert_eq!(
            resolved.link,
            Some((1, "https://example.com/survives".into()))
        );
        // No back-fill: the corrupt payload is still there, untouched.
        let raw: Option<String> = conn.get(redis.redis_key(&slug)).await.unwrap();
        assert_eq!(raw.as_deref(), Some("{ not-valid-Cached-json"));

        redis.cleanup().await;
    }
}
