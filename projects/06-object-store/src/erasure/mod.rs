//! Erasure coding lab — From the field (scaffold).
//!
//! Survive lost disks for a fraction of replication's cost: split a blob into
//! `k` data shards + `m` parity shards so **any `k` of `n = k+m`** rebuild the
//! original bit-exact. This module is a **codec lab**, not a Store backend yet —
//! identity stays the plaintext SHA-256; wiring shards under `objects/` is a
//! later step (see doc §8).
//!
//! ## Build order (do these in sequence)
//!
//! 1. [`gf256`] — GF(2⁸) with `0x11D` reduction + log/antilog tables
//! 2. [`reed_solomon`] — systematic RS(4,2) encode / reconstruct
//! 3. [`lrc`] — Local Reconstruction Codes on top of RS (cheap single-shard repair)
//! 4. [`durability`] — Backblaze-style nines calculator from `(k, m, AFR, window)`
//!
//! Teach-yourself: [`docs/12-how-erasure-coding-works.md`](../../docs/12-how-erasure-coding-works.md).
//! SPEC backlog: Storage-engine labs (RS → LRC → durability calculator).
//!
//! ## Status
//!
//! All four layers are implemented and pass `tests/erasure_acceptance.rs`. This
//! stays a **codec lab**: default PUT/GET still use FileCas / Haystack — nothing
//! here is on the request path yet (wiring shards under `objects/` is doc §8).

pub mod durability;
pub mod gf256;
pub mod lrc;
pub mod reed_solomon;

pub use durability::{compute_durability, DurabilityInput, DurabilityReport};
pub use gf256::Gf256;
pub use lrc::{Lrc, LrcParams, RepairStats};
pub use reed_solomon::{ReedSolomon, Shard, ShardId, RS_K, RS_M, RS_N};

use thiserror::Error;

/// Errors from the erasure codec (not mapped to S3 HTTP — this lab is offline).
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ErasureError {
    /// A GF multiply/divide hit a zero denominator, or a matrix was singular.
    #[error("singular or undefined GF(2⁸) operation: {0}")]
    Singular(String),

    /// Not enough surviving shards to reconstruct (`need k`, got fewer).
    #[error("too many erasures: need {need} shards, have {have}")]
    TooManyErasures { need: usize, have: usize },

    /// Shard lengths disagree, or data length is not compatible with `k`.
    #[error("invalid shard layout: {0}")]
    InvalidLayout(String),

    /// Caller asked for a parameter the scaffold does not support yet.
    #[error("unsupported parameters: {0}")]
    Unsupported(String),
}
