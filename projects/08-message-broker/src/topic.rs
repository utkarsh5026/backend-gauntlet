//! V3 — Topics & partitioning: trade global ordering for parallelism.
//!
//! A `Topic` is **N independent partition logs**. The single interesting decision
//! lives in `partition_for`: which partition does a record go to?
//!   - **Keyed** records hash their key to a partition, so the *same key always
//!     lands on the same partition* — that's what preserves per-key order across
//!     a producer's lifetime.
//!   - **Keyless** records spread (round-robin) so no partition runs hot.
//!
//! The guarantee this buys, and its price, are the `Done when` criteria: order is
//! total *within* a partition and *undefined across* partitions; offsets are
//! per-partition. Partition count is fixed at create time — changing it would
//! remap every key, so it's a migration, not a setting.

use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicU64;
use std::sync::Arc;

use crate::error::AppError;
use crate::log::LogConfig;
use crate::partition::Partition;
use crate::record::{Offset, Record};

/// A topic: a name and its fixed set of partition logs.
pub struct Topic {
    name: String,
    partitions: Vec<Arc<Partition>>,
    /// Cursor for round-robin placement of keyless records.
    next_rr: AtomicU64,
}

impl Topic {
    /// Create a new topic with `partition_count` partitions under `root`
    /// (`root/<name>/<p>/`). Errors if the topic directory already exists.
    pub fn create(
        root: &Path,
        name: &str,
        partition_count: u32,
        config: LogConfig,
    ) -> Result<Arc<Self>, AppError> {
        if partition_count == 0 {
            return Err(AppError::InvalidRequest(
                "partition count must be ≥ 1".into(),
            ));
        }
        let dir = root.join(name);
        if dir.exists() {
            return Err(AppError::TopicAlreadyExists);
        }
        let partitions = open_partitions(&dir, partition_count, config)?;
        Ok(Arc::new(Self {
            name: name.to_string(),
            partitions,
            next_rr: AtomicU64::new(0),
        }))
    }

    /// Reopen an existing topic on startup by counting its partition directories.
    pub fn open(root: &Path, name: &str, config: LogConfig) -> Result<Arc<Self>, AppError> {
        let dir = root.join(name);
        let count = std::fs::read_dir(&dir)?
            .filter_map(|e| e.ok())
            .filter(|e| e.path().is_dir())
            .count() as u32;
        let partitions = open_partitions(&dir, count.max(1), config)?;
        Ok(Arc::new(Self {
            name: name.to_string(),
            partitions,
            next_rr: AtomicU64::new(0),
        }))
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn partition_count(&self) -> usize {
        self.partitions.len()
    }

    /// Look up a partition by index (used by the fetch route).
    pub fn partition(&self, id: u32) -> Result<&Arc<Partition>, AppError> {
        self.partitions
            .get(id as usize)
            .ok_or(AppError::UnknownPartition)
    }

    /// Choose the partition for a record: hash the key, or round-robin if keyless.
    /// **The** V3 decision.
    pub fn partition_for(&self, key: Option<&[u8]>) -> u32 {
        // TODO(V3): if `key` is Some, hash it to a stable partition
        //   (hash(key) % partition_count) — the same key must always map here for
        //   the life of the topic, so pick a hash that doesn't change run to run.
        //   If `key` is None, take the next round-robin slot via `next_rr`.
        let _ = (key, &self.next_rr, self.partitions.len());
        todo!("V3: keyed hash-partitioning + keyless round-robin")
    }

    /// Produce a record to the topic: pick a partition, append, and return where
    /// it landed. Wired on top of the V3 partitioner + the V1 append.
    pub async fn produce(&self, record: Record) -> Result<(u32, Offset), AppError> {
        let key = record.key.as_deref();
        let p = self.partition_for(key);
        let partition = self.partition(p)?;
        let offset = partition.append(&record).await?;
        Ok((p, offset))
    }
}

/// Open partitions `0..count` under `dir`, each in its own subdirectory.
fn open_partitions(
    dir: &Path,
    count: u32,
    config: LogConfig,
) -> Result<Vec<Arc<Partition>>, AppError> {
    let dir: PathBuf = dir.to_path_buf();
    (0..count)
        .map(|id| Partition::open(dir.join(id.to_string()), id, config))
        .collect()
}
