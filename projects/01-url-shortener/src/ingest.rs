//! Background click-ingestion task (V3).
//!
//! Batches [`ClickEvent`]s off the redirect hot path and bulk-writes them to
//! Postgres, so a redirect never blocks on a DB write. Clicks are analytics
//! data: lossy by design. On any flush error we log and drop the batch rather
//! than retry — losing a batch on a rare DB blip is noise, and retrying buys
//! complexity we don't need here.
//!
//! [`ClickIngestor::new`] hands back two halves of the same channel:
//! - the [`ClickIngestor`] consumer (owns `db` + receiver) — spawn its [`run`]
//!   loop once;
//! - a cheap-to-clone [`ClickSink`] handle (owns the sender) — stash it in
//!   `AppState` so every handler can [`accept`] clicks.
//!
//! [`run`]: ClickIngestor::run
//! [`accept`]: ClickSink::accept

use std::time::Duration;

use sqlx::{PgPool, Postgres, QueryBuilder};
use tracing::{debug, warn};

/// A single recorded click, handed off to the background ingestion task so the
/// redirect hot path never blocks on a DB write (V3).
#[derive(Debug, Clone)]
pub struct ClickEvent {
    pub link_id: i64,
    pub referer: Option<String>,
    pub user_agent: Option<String>,
    pub ip_hash: Option<String>,
}

const MAX_BATCH: usize = 500;
const FLUSH_EVERY: Duration = Duration::from_millis(500);
const CHANNEL_CAPACITY: usize = 10_000;

/// Cheap-to-clone producer handle. Lives in `AppState`; the redirect path uses
/// it to hand clicks to the background ingestor without blocking.
#[derive(Clone)]
pub struct ClickSink {
    tx: tokio::sync::mpsc::Sender<ClickEvent>,
}

impl ClickSink {
    /// Hand a click to the ingestor. Non-blocking and lossy by design: if the
    /// buffer is full we drop the event rather than stall the redirect.
    pub fn accept(&self, event: ClickEvent) {
        let _ = self.tx.try_send(event);
    }
}

/// Background consumer: drains the channel, batching by size or time, and
/// bulk-inserts. Owns the receiver, so there is exactly one of these — spawn
/// [`run`](Self::run) once at startup.
pub struct ClickIngestor {
    db: PgPool,
    rx: tokio::sync::mpsc::Receiver<ClickEvent>,
}

impl ClickIngestor {
    /// Build the consumer and its paired [`ClickSink`] handle. Spawn the
    /// returned ingestor's [`run`](Self::run); clone the sink into `AppState`.
    pub fn new(db: PgPool) -> (Self, ClickSink) {
        let (tx, rx) = tokio::sync::mpsc::channel::<ClickEvent>(CHANNEL_CAPACITY);
        (Self { db, rx }, ClickSink { tx })
    }

    /// Run the batching loop until every [`ClickSink`] is dropped (channel
    /// closed), flushing on a full batch, on each tick, and once more on exit.
    pub async fn run(mut self) {
        let mut buf: Vec<ClickEvent> = Vec::with_capacity(MAX_BATCH);
        let mut ticker = tokio::time::interval(FLUSH_EVERY);

        loop {
            tokio::select! {
                maybe = self.rx.recv() => match maybe {
                    Some(e) => {
                        buf.push(e);
                        if buf.len() >= MAX_BATCH {
                            self.flush(&mut buf).await;
                        }
                    }
                    None => { // all senders dropped: final flush and exit
                        self.flush(&mut buf).await;
                        break;
                    }
                },
                _ = ticker.tick() => self.flush(&mut buf).await,
            }
        }
        debug!("click ingestor stopped");
    }

    /// Bulk-insert the batch in one statement, then clear it. On error we log and
    /// drop — clicks are analytics data we can afford to lose.
    async fn flush(&mut self, buf: &mut Vec<ClickEvent>) {
        if buf.is_empty() {
            return;
        }
        let n = buf.len();

        let mut qb = QueryBuilder::<Postgres>::new(
            "INSERT INTO click_events (link_id, referer, user_agent, ip_hash) ",
        );
        qb.push_values(buf.iter(), |mut b, event| {
            b.push_bind(event.link_id)
                .push_bind(&event.referer)
                .push_bind(&event.user_agent)
                .push_bind(&event.ip_hash);
        });

        match qb.build().execute(&self.db).await {
            Ok(r) => debug!(count = n, rows = r.rows_affected(), "flushed click batch"),
            Err(e) => warn!(count = n, error = %e, "dropping click batch (events lost)"),
        }
        buf.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sqlx::PgPool;

    async fn insert_link(pool: &PgPool, id: i64, slug: &str) {
        sqlx::query!(
            "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)",
            id,
            slug,
            "https://example.com"
        )
        .execute(pool)
        .await
        .expect("seed link row");
    }

    fn click(link_id: i64) -> ClickEvent {
        ClickEvent {
            link_id,
            referer: None,
            user_agent: Some("test-agent".into()),
            ip_hash: None,
        }
    }

    async fn count_clicks(pool: &PgPool) -> i64 {
        sqlx::query_scalar!("SELECT COUNT(*) FROM click_events")
            .fetch_one(pool)
            .await
            .unwrap()
            .unwrap_or(0)
    }

    #[sqlx::test]
    async fn flush_bulk_inserts_then_clears_batch(pool: PgPool) {
        insert_link(&pool, 1, "seed").await;
        let (mut ingestor, _sink) = ClickIngestor::new(pool.clone());

        let mut buf = vec![click(1), click(1), click(1)];
        ingestor.flush(&mut buf).await;

        assert!(buf.is_empty(), "a flush drains the batch");
        assert_eq!(count_clicks(&pool).await, 3, "all rows committed in one insert");
    }

    #[sqlx::test]
    async fn flush_empty_batch_is_a_noop(pool: PgPool) {
        let (mut ingestor, _sink) = ClickIngestor::new(pool.clone());
        let mut buf: Vec<ClickEvent> = Vec::new();

        ingestor.flush(&mut buf).await; // must not touch the DB or panic

        assert!(buf.is_empty());
        assert_eq!(count_clicks(&pool).await, 0);
    }

    #[sqlx::test]
    async fn flush_drops_a_failing_batch_and_survives(pool: PgPool) {
        // No link with id 999 exists → FK violation. The batch is dropped, the
        // buffer cleared, and the ingestor keeps going.
        let (mut ingestor, _sink) = ClickIngestor::new(pool.clone());

        let mut buf = vec![click(999)];
        ingestor.flush(&mut buf).await;

        assert!(buf.is_empty(), "a failing batch is dropped, not stuck");
        assert_eq!(count_clicks(&pool).await, 0, "the atomic insert committed nothing");
    }

    #[sqlx::test]
    async fn run_loop_drains_and_flushes_on_shutdown(pool: PgPool) {
        insert_link(&pool, 1, "seed").await;
        let (ingestor, sink) = ClickIngestor::new(pool.clone());
        let handle = tokio::spawn(ingestor.run());

        sink.accept(click(1));
        sink.accept(click(1));

        // Dropping the only sink closes the channel; `run` does a final flush and
        // exits. Awaiting the task makes the assertion deterministic — no reliance
        // on the flush ticker firing.
        drop(sink);
        handle.await.expect("ingestor task joins cleanly");

        assert_eq!(count_clicks(&pool).await, 2, "buffered clicks flushed on drain");
    }
}
