//! The LSM tree: where the verticals compose into one key/value store.
//!
//! This is the orchestrator the RESP command layer ([`crate::server`]) and the HTTP
//! sidecar call. It owns the write path (WAL → memtable → flush) and the read path
//! (memtable → frozen memtables → SSTables, reconciled by recency), and it holds the
//! block cache and the sequence counter that orders every write.
//!
//! [`Engine::open`] is **fully wired** — on a fresh data directory it constructs an
//! empty, serving engine (so the bare scaffold runs). The interesting methods —
//! [`get`](Engine::get), [`set`](Engine::set), [`delete`](Engine::delete),
//! [`flush_memtable`](Engine::flush_memtable), [`run_compaction`](Engine::run_compaction)
//! — are the `todo!()`s where the verticals meet. Read-path reconciliation (newest
//! wins; a tombstone stops the search) and write-path flushing are the *meat*, so they
//! stay yours to write.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};

use bytes::Bytes;
use serde::Serialize;
use tracing::info;

use crate::block_cache::BlockCache;
use crate::memtable::Memtable;
use crate::sstable::SsTable;
use crate::wal::{SyncPolicy, Wal};

/// Tunables, read from the environment in `main`. Redis exposes the analogous dials as
/// `appendfsync`, and RocksDB as `write_buffer_size` / `level0_file_num_compaction_trigger`
/// / `block_cache_size` — the names differ, the tradeoffs are the same.
#[derive(Debug, Clone)]
pub struct EngineConfig {
    /// Where the WAL + SSTable files live. This directory *is* the database.
    pub data_dir: PathBuf,
    /// Freeze + flush the memtable once it holds this many approximate bytes (V3→V4).
    pub memtable_max_bytes: usize,
    /// When the WAL forces bytes to disk (V2).
    pub wal_sync: SyncPolicy,
    /// Target size of an SSTable data block (V4) — the read/cache granularity.
    pub block_size_bytes: usize,
    /// Bloom filter sizing (V5); ~10 gives a ~1% false-positive rate.
    pub bloom_bits_per_key: usize,
    /// Compact once the youngest level holds this many SSTables (V6).
    pub l0_compaction_trigger: usize,
    /// Block cache budget in bytes (V7); 0 disables the cache.
    pub block_cache_bytes: usize,
}

/// A snapshot of engine internals for `/stats` and the metrics gauges. Fully wired.
#[derive(Debug, Clone, Serialize)]
pub struct EngineStats {
    pub keys_memtable: usize,
    pub memtable_bytes: usize,
    pub immutable_memtables: usize,
    pub sstables: usize,
    pub block_cache_capacity_bytes: usize,
    pub block_cache_hits: u64,
    pub block_cache_misses: u64,
    pub sequence: u64,
}

pub struct Engine {
    config: EngineConfig,
    /// The durable log. Writes serialize through it (append-before-ack), so it's behind
    /// its own mutex, independent of the read locks.
    wal: Mutex<Wal>,
    /// The active in-memory write buffer (V3).
    memtable: RwLock<Memtable>,
    /// Frozen memtables awaiting a background flush to an SSTable (V4). Read after the
    /// active memtable, before the SSTables.
    immutable: RwLock<Vec<Arc<Memtable>>>,
    /// On-disk sorted runs, newest-first for now; V6 organizes these into levels.
    sstables: RwLock<Vec<Arc<SsTable>>>,
    /// The shared, hand-built block cache (V7).
    block_cache: Arc<BlockCache>,
    /// Monotonic sequence number stamped on every write (newest wins across levels).
    seq: AtomicU64,
    /// Next SSTable file id to hand out.
    next_sstable_id: AtomicU64,
}

impl Engine {
    /// Open (or create) the store rooted at `config.data_dir`.
    ///
    /// Wired end-to-end: creates the directory, opens the WAL, replays it **only if it
    /// is non-empty** (a fresh log needs no V2 replay, so the bare scaffold never trips
    /// the `todo!()`), discovers existing SSTable files, and builds the block cache.
    pub fn open(config: EngineConfig) -> Result<Arc<Engine>, crate::error::AppError> {
        std::fs::create_dir_all(&config.data_dir)?;

        let wal_path = config.data_dir.join("wal.log");
        let wal = Wal::open(&wal_path, config.wal_sync)?;

        // Rebuild the memtable from the WAL — but only when there is something to
        // rebuild. On a fresh/empty log we skip straight past V2's replay `todo!()`.
        let mut memtable = Memtable::new();
        let mut max_seq = 0u64;
        if wal.len_bytes()? > 0 {
            for rec in Wal::replay(&wal_path)? {
                max_seq = max_seq.max(rec.seq);
                let value = match rec.op {
                    crate::wal::Op::Set => {
                        crate::memtable::Value::Value(rec.value.unwrap_or_default())
                    }
                    crate::wal::Op::Delete => crate::memtable::Value::Tombstone,
                };
                memtable.insert(rec.key, value, rec.seq);
            }
        }

        // Discover existing SSTables. Empty on a fresh directory, so V4's `open`
        // `todo!()` is only reached once you've actually flushed some.
        let mut sstables = Vec::new();
        let mut max_id = 0u64;
        for entry in std::fs::read_dir(&config.data_dir)? {
            let path = entry?.path();
            if path.extension().and_then(|e| e.to_str()) == Some("sst") {
                let id: u64 = path
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
                max_id = max_id.max(id);
                sstables.push(Arc::new(SsTable::open(&path, id)?));
            }
        }

        let block_cache = Arc::new(BlockCache::new(config.block_cache_bytes));

        info!(
            data_dir = %config.data_dir.display(),
            recovered_keys = memtable.len(),
            sstables = sstables.len(),
            "engine opened"
        );

        Ok(Arc::new(Engine {
            config,
            wal: Mutex::new(wal),
            memtable: RwLock::new(memtable),
            immutable: RwLock::new(Vec::new()),
            sstables: RwLock::new(sstables),
            block_cache,
            seq: AtomicU64::new(max_seq),
            next_sstable_id: AtomicU64::new(max_id + 1),
        }))
    }

    /// The next monotonic sequence number for a write. Wired helper for the write path.
    fn next_seq(&self) -> u64 {
        self.seq.fetch_add(1, Ordering::SeqCst) + 1
    }

    /// `GET key` — the newest value for `key`, or `None` if absent/deleted.
    ///
    /// TODO(read path — V3→V4→V7): reconcile across levels **newest-first**: the active
    /// memtable, then each frozen memtable, then SSTables newest→oldest. The *first*
    /// level with an opinion wins — and a tombstone is an opinion: it means "deleted,"
    /// so return `None` and stop, do not fall through to an older SSTable that still has
    /// the key. SSTable lookups go through the bloom (V5) + block cache (V7).
    pub async fn get(&self, key: &[u8]) -> Result<Option<Bytes>, crate::error::AppError> {
        let _ = (
            key,
            &self.memtable,
            &self.immutable,
            &self.sstables,
            &self.block_cache,
        );
        todo!("read path: memtable → frozen → SSTables (newest wins, tombstone stops the search)")
    }

    /// `SET key value` — record a write durably, then buffer it.
    ///
    /// TODO(write path — V2→V3): stamp the write with [`next_seq`](Engine::next_seq),
    /// append it to the WAL (V2, ack only after the sync policy is satisfied), then
    /// insert it into the active memtable (V3). If the memtable is now full
    /// (`>= memtable_max_bytes`), freeze it into `immutable` and start a fresh one +
    /// a fresh WAL segment — hand the frozen one to [`flush_memtable`](Engine::flush_memtable)
    /// in the background. Freezing (not blocking) is what keeps writes flowing.
    pub async fn set(&self, key: Bytes, value: Bytes) -> Result<(), crate::error::AppError> {
        let _ = (key, value, &self.wal, self.config.memtable_max_bytes);
        todo!("write path: WAL append (V2) → memtable insert (V3) → maybe freeze + flush")
    }

    /// `DEL key` — returns `true` if the key existed. A delete is a **tombstone write**,
    /// not an erase.
    ///
    /// TODO(write path — V2→V3): same shape as [`set`](Engine::set) but appends a
    /// `Delete` (tombstone) at a fresh seq. Whether you report `true`/`false` requires a
    /// read to know if the key was live — decide the cost/consistency of that and note
    /// it in `docs/22-design.md`.
    pub async fn delete(&self, key: &[u8]) -> Result<bool, crate::error::AppError> {
        let _ = (key, &self.wal);
        todo!("write path: append a tombstone (V2) + insert it into the memtable (V3)")
    }

    /// Flush a frozen memtable to a new on-disk SSTable and retire its WAL segment.
    ///
    /// TODO(V4): allocate an id via `next_sstable_id`, call
    /// [`SsTable::create`](crate::sstable::SsTable::create) over the frozen memtable's
    /// sorted entries, publish the new table into `sstables`, drop the frozen memtable,
    /// and only *then* delete/rotate the WAL segment that covered it — deleting the WAL
    /// before the SSTable is durable is how you lose data across a crash.
    pub async fn flush_memtable(&self) -> Result<(), crate::error::AppError> {
        let _ = (
            &self.sstables,
            &self.immutable,
            self.config.block_size_bytes,
            self.config.bloom_bits_per_key,
            self.next_sstable_id.load(Ordering::SeqCst),
        );
        todo!("V4: write the frozen memtable to an SSTable, publish it, then retire its WAL")
    }

    /// Compact SSTables to bound read + space amplification (V6). Called by the
    /// background loop (off by default) — see [`crate::compaction`].
    ///
    /// TODO(V6): if the youngest level holds `>= l0_compaction_trigger` tables, pick a
    /// compaction, merge the chosen SSTables into fewer/larger ones (dropping shadowed
    /// values and tombstones with nothing older beneath them), install the outputs, and
    /// delete the inputs. Returns whether any work was done.
    pub async fn run_compaction(&self) -> Result<bool, crate::error::AppError> {
        let _ = (
            self.config.l0_compaction_trigger,
            &self.sstables,
            &self.block_cache,
        );
        todo!("V6: pick + merge SSTables, drop shadowed keys/tombstones, swap outputs in")
    }

    /// A consistent snapshot of engine internals. Fully wired — powers `/stats`,
    /// `/healthz`, and the metrics gauges on the bare scaffold.
    pub fn stats(&self) -> EngineStats {
        let (hits, misses) = self.block_cache.stats();
        let memtable = self.memtable.read().expect("memtable lock poisoned");
        EngineStats {
            keys_memtable: memtable.len(),
            memtable_bytes: memtable.approx_bytes(),
            immutable_memtables: self
                .immutable
                .read()
                .expect("immutable lock poisoned")
                .len(),
            sstables: self.sstables.read().expect("sstables lock poisoned").len(),
            block_cache_capacity_bytes: self.block_cache.capacity_bytes(),
            block_cache_hits: hits,
            block_cache_misses: misses,
            sequence: self.seq.load(Ordering::SeqCst),
        }
    }
}
