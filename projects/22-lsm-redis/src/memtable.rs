//! V3 — Memtable: the sorted, in-memory write buffer. `src/memtable.rs`.
//!
//! Every write lands here first (right after the WAL append). The memtable is the "L"
//! of the LSM tree — a **log-structured** engine turns random writes into sequential
//! ones by buffering them sorted in memory and only ever writing whole, immutable,
//! sorted files to disk (SSTables, V4). To make that flush cheap, the buffer must
//! already be **sorted by key**, so this is an ordered map (a `BTreeMap`, or a skip
//! list if you want the classic).
//!
//! Three semantics beyond "a map":
//!
//!   1. **Tombstones.** A delete inserts a [`Value::Tombstone`], not a removal. Older
//!      values for that key still live in SSTables on disk; the tombstone *shadows*
//!      them on read until compaction (V6) finally drops both. Removing the key from
//!      the map would un-delete it on the next read from disk.
//!   2. **Sequence numbers.** Each entry carries the write's `seq` (from the WAL) so
//!      that when the same key exists in the memtable *and* several SSTables, the read
//!      path can pick the newest. Within one memtable, a later write simply overwrites.
//!   3. **Size accounting + rotation.** You track approximate bytes held. When it
//!      crosses `MEMTABLE_MAX_BYTES`, the memtable is **frozen** (made immutable) and a
//!      fresh one takes over writes; the frozen one is flushed to an SSTable in the
//!      background. Freezing instead of blocking is what keeps writes flowing during a
//!      flush — get the handoff wrong and you get the write stall (the boss).
//!
//! *Concept to internalize:* why LSM trades read simplicity for write throughput
//! (sequential, sorted flushes vs a B-tree's in-place random writes), and why a delete
//! in a log-structured store is an *append*, never an in-place erase.

use std::collections::BTreeMap;

use bytes::Bytes;

/// What a key maps to in the buffer: a live value or a tombstone, tagged with the
/// sequence number of the write that produced it (newest wins across levels).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    Value(Bytes),
    Tombstone,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub seq: u64,
    pub value: Value,
}

/// A sorted, in-memory write buffer. `new`/`len` are wired; the mutation + lookup +
/// size accounting are V3.
#[derive(Default)]
pub struct Memtable {
    map: BTreeMap<Bytes, Entry>,
    /// Approximate heap bytes held (keys + values + per-entry overhead) — the number
    /// [`is_full`](Memtable::is_full) compares against the flush threshold.
    approx_bytes: usize,
}

impl Memtable {
    pub fn new() -> Self {
        Memtable::default()
    }

    /// Number of distinct keys currently buffered (a live key and a tombstone both
    /// count — a tombstone is an entry). Wired, for `/stats`.
    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    /// Approximate bytes held — the rotation trigger.
    pub fn approx_bytes(&self) -> usize {
        self.approx_bytes
    }

    /// True once the buffer has grown past `max_bytes` and should be frozen + flushed.
    pub fn is_full(&self, max_bytes: usize) -> bool {
        self.approx_bytes >= max_bytes
    }

    /// Apply a write (a `SET` value or a `Delete` tombstone) at sequence `seq`.
    ///
    /// TODO(V3): insert/overwrite `key`'s entry and keep [`approx_bytes`](Memtable::approx_bytes)
    /// in step — add the new footprint, subtract any entry you replaced. A `Delete` stores
    /// a [`Value::Tombstone`]; it does **not** remove the key. A later `seq` for a key
    /// supersedes an earlier one.
    pub fn insert(&mut self, key: Bytes, value: Value, seq: u64) {
        let _ = (key, value, seq);
        todo!("V3: upsert the entry (tombstone on delete) and update approx_bytes")
    }

    /// Look up a key *in this memtable only*.
    ///
    /// Returns `Some(&Entry)` if this buffer has an opinion about the key — including a
    /// tombstone (which the read path reads as "deleted, stop looking at older SSTables").
    /// `None` means "not here; fall through to the next level."
    ///
    /// TODO(V3): probe the ordered map. (Trivial over a `BTreeMap` — the learning is
    /// realizing a tombstone hit is a *positive* answer to the read path, not a miss.)
    pub fn get(&self, key: &[u8]) -> Option<&Entry> {
        let _ = key;
        todo!("V3: return this memtable's entry for the key, tombstone included")
    }

    /// Drain the buffer in **key order** as `(key, entry)` — exactly the sequence an
    /// SSTable writer (V4) consumes to produce a sorted file. Wired so V4 can iterate.
    pub fn iter_sorted(&self) -> impl Iterator<Item = (&Bytes, &Entry)> {
        self.map.iter()
    }
}

#[cfg(test)]
mod tests {
    // TODO(V3): prove the buffer's semantics.
    //   - ordering: `iter_sorted` yields keys ascending regardless of insert order
    //     (a proptest over shuffled inserts);
    //   - last-write-wins: two SETs to one key → `get` returns the higher-seq value;
    //   - tombstone: SET then DELETE a key → `get` returns a `Tombstone` (not `None`),
    //     so the read path knows to stop, not fall through to disk;
    //   - accounting: `approx_bytes` rises on insert of a new key and does not
    //     double-count an overwrite; `is_full` flips exactly at the threshold.
}
