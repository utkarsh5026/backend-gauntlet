//! V6 — Compaction: the "M" that keeps an LSM from drowning in its own writes.
//!
//! Every flush adds another SSTable. Left alone, the same key accumulates copies across
//! dozens of files, a read has to check all of them (**read amplification**), deleted
//! keys never actually free their space (**space amplification**), and eventually
//! flushes outrun the disk and the engine has to *stop accepting writes* — the write
//! stall (the boss). **Compaction** is the background housekeeping that fixes all three:
//! merge several sorted SSTables into fewer, larger sorted ones, keeping only the newest
//! value per key and dropping tombstones once nothing older survives beneath them.
//!
//! The *policy* is the design choice, and it's a genuine tradeoff:
//!   - **Size-tiered** (Cassandra) merges same-size runs → cheap writes, worse read/space
//!     amplification.
//!   - **Leveled** (LevelDB/RocksDB) keeps each level non-overlapping and ~10× the one
//!     above → tighter reads/space, more write amplification.
//! Pick one, size it with `L0_COMPACTION_TRIGGER`, and justify it in `docs/22-design.md`.
//!
//! This module is the **loop** (wired, like the memtable-flush or refresh loops
//! elsewhere in the gauntlet); the *decision and the merge* live in
//! [`Engine::run_compaction`](crate::engine::Engine::run_compaction) (V6). The loop is
//! spawned only when `RUN_COMPACTION=true`, so the bare scaffold never reaches the
//! `todo!()`. Turn it on once V4 (flush) and V6 (merge) exist.
//!
//! *Concept to internalize:* the write/read/space amplification triangle — you cannot
//! minimize all three, and the compaction policy is exactly where you choose which to
//! favor. Compaction is also what makes deletes eventually reclaim disk.

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;

use crate::engine::Engine;

/// Background compaction: periodically ask the engine to compact if a level is over its
/// trigger, until shutdown. Wired — the `select!`/interval/shutdown skeleton is done;
/// what a compaction *does* is [`Engine::run_compaction`] (V6).
pub async fn compaction_loop(
    engine: Arc<Engine>,
    interval: Duration,
    mut shutdown: watch::Receiver<bool>,
) {
    let mut ticker = tokio::time::interval(interval);
    loop {
        tokio::select! {
            _ = ticker.tick() => {
                match engine.run_compaction().await {
                    Ok(true) => tracing::info!("compaction ran"),
                    Ok(false) => {} // nothing to do this tick
                    Err(e) => tracing::warn!(error = %e, "compaction failed"),
                }
            }
            _ = shutdown.changed() => {
                tracing::info!("compaction loop shutting down");
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    // TODO(V6): prove compaction bounds amplification and preserves the data.
    //   - correctness: after compacting SSTables that all touch key K, a read returns
    //     K's newest value and the older copies are gone from disk;
    //   - tombstone reclamation: a deleted key with nothing older beneath it is fully
    //     dropped by compaction (it stops appearing on disk), and disk usage falls;
    //   - bound: under a sustained write load with the loop running, the youngest-level
    //     SSTable count stays within a small factor of `L0_COMPACTION_TRIGGER` — it does
    //     not grow without bound (this is the anti-write-stall invariant the boss checks);
    //   - a tombstone is NOT dropped while an older SSTable outside the compaction set
    //     still holds the key (dropping it early would resurrect the value).
}
