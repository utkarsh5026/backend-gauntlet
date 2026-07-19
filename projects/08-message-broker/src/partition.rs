//! One partition = one log behind a lock. Plumbing, not a vertical.
//!
//! A partition owns a single `Log` (V1/V2) guarded by a `Mutex`, which encodes
//! the broker's concurrency model: **appends to a partition serialize** (one
//! writer at a time), while different partitions append in parallel. The lock is
//! held across a read too — a deliberate simplification a real broker would relax
//! (see the "cross-cutting scale skills" horizontal item).

use std::path::Path;
use std::sync::Arc;

use tokio::sync::Mutex;

use crate::error::AppError;
use crate::log::{Log, LogConfig};
use crate::record::{Offset, Record, StoredRecord};

/// A single partition: an id within its topic plus the log holding its records.
pub struct Partition {
    id: u32,
    log: Mutex<Log>,
}

impl Partition {
    /// Open (creating if needed) partition `id`'s log under `dir`.
    pub fn open(dir: impl AsRef<Path>, id: u32, config: LogConfig) -> Result<Arc<Self>, AppError> {
        let log = Log::open(dir, config)?;
        Ok(Arc::new(Self {
            id,
            log: Mutex::new(log),
        }))
    }

    /// This partition's index within its topic.
    pub fn id(&self) -> u32 {
        self.id
    }

    /// Append a record, returning the offset it was assigned (delegates to V1).
    pub async fn append(&self, record: &Record) -> Result<Offset, AppError> {
        self.log.lock().await.append(record).await
    }

    /// Read up to `max_records` records starting at `offset` (delegates to V1/V2).
    pub async fn read_from(
        &self,
        offset: Offset,
        max_records: usize,
    ) -> Result<Vec<StoredRecord>, AppError> {
        self.log.lock().await.read_from(offset, max_records).await
    }

    /// The next offset to be assigned == log-end offset (used for consumer lag).
    pub async fn log_end_offset(&self) -> Offset {
        self.log.lock().await.log_end_offset()
    }
}
