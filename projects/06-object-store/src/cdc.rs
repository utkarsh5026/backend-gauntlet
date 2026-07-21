//! Content-defined chunking (CDC) — From the field (ungraded).
//!
//! **CDC here means content-defined chunking**, not Change Data Capture.
//!
//! Whole-object CAS (V1) only shares *identical* files. This module is the
//! cutter that splits a plaintext byte stream at content-chosen boundaries so
//! *similar* objects can share most on-disk chunks. See
//! [`docs/10-how-chunk-level-dedup-works.md`](../docs/10-how-chunk-level-dedup-works.md).
//!
//! ## Cutter implementation
//!
//! [`CdcChunker`] wraps [`fastcdc::v2020`](https://docs.rs/fastcdc/latest/fastcdc/v2020/)
//! (Gear hash / FastCDC 2020). The crate picks **cut points**; you still SHA-256
//! each plaintext chunk and CAS-commit it elsewhere
//! ([`crate::streaming::stream_cdc_to_store`] — still a `todo!()`).
//!
//! Streaming shape: network frames are buffered; we only emit a chunk when
//! FastCDC finds a cut *before* the buffer end, or the buffer has reached
//! `max_size` (forced cut). A cut that consumes the whole buffer while
//! `len < max_size` waits for more bytes or [`finish`](CdcChunker::finish).
//!
//! Identity stays **plaintext** digest (hash-then-compress). Compression, if
//! any, is a physical encoding of a chunk — same rule as the cold tier.
//!
//! Default PUT stays on [`crate::streaming::stream_to_store`] until
//! [`CdcConfig::enabled`] is set on [`crate::AppState`]
//! (via [`crate::AppState::with_cdc`] from `main`).

use fastcdc::v2020::FastCDC;

use crate::error::AppError;

/// Default minimum chunk size (8 KiB).
pub const DEFAULT_MIN_SIZE: usize = 8 * 1024;

/// Default target average chunk size (64 KiB).
pub const DEFAULT_AVG_SIZE: usize = 64 * 1024;

/// Default maximum chunk size (256 KiB).
pub const DEFAULT_MAX_SIZE: usize = 256 * 1024;

/// Default minimum object size before CDC is worthwhile (256 KiB).
pub const DEFAULT_MIN_OBJECT_SIZE: u64 = 256 * 1024;

/// Process-wide CDC settings — parsed once at boot, held on [`crate::AppState`].
///
/// Routes read `state.cdc`, not the environment, so tests stay on whole-object
/// CAS unless they call [`crate::AppState::with_cdc`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CdcConfig {
    /// When false, PUT always uses whole-object [`crate::streaming::stream_to_store`].
    pub enabled: bool,
    /// Never emit a chunk shorter than this (except the final EOF chunk).
    pub min_size: usize,
    /// Target average chunk size (drives the FastCDC mask).
    pub avg_size: usize,
    /// Force a cut if a chunk grows this large without a natural boundary.
    pub max_size: usize,
    /// Skip CDC for objects smaller than this — one whole-object blob is cheaper.
    pub min_object_size: u64,
}

impl Default for CdcConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            min_size: DEFAULT_MIN_SIZE,
            avg_size: DEFAULT_AVG_SIZE,
            max_size: DEFAULT_MAX_SIZE,
            min_object_size: DEFAULT_MIN_OBJECT_SIZE,
        }
    }
}

impl CdcConfig {
    /// Read from the environment; missing keys keep [`Default`] values.
    ///
    /// Call from `main` and install with [`crate::AppState::with_cdc`] — do not
    /// re-read env inside request handlers.
    ///
    /// | Env | Field |
    /// | --- | --- |
    /// | `CDC_ENABLED` | `enabled` (`1` / `true` / `yes`) |
    /// | `CDC_MIN_CHUNK` | `min_size` |
    /// | `CDC_AVG_CHUNK` | `avg_size` |
    /// | `CDC_MAX_CHUNK` | `max_size` |
    /// | `CDC_MIN_OBJECT` | `min_object_size` |
    pub fn from_env() -> Self {
        let env_usize = |key: &str| -> Option<usize> { std::env::var(key).ok()?.parse().ok() };
        let env_u64 = |key: &str| -> Option<u64> { std::env::var(key).ok()?.parse().ok() };
        Self {
            enabled: match std::env::var("CDC_ENABLED") {
                Ok(v) => matches!(v.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"),
                Err(_) => false,
            },
            min_size: env_usize("CDC_MIN_CHUNK").unwrap_or(DEFAULT_MIN_SIZE),
            avg_size: env_usize("CDC_AVG_CHUNK").unwrap_or(DEFAULT_AVG_SIZE),
            max_size: env_usize("CDC_MAX_CHUNK").unwrap_or(DEFAULT_MAX_SIZE),
            min_object_size: env_u64("CDC_MIN_OBJECT").unwrap_or(DEFAULT_MIN_OBJECT_SIZE),
        }
    }

    /// Whether an object of `logical_size` bytes should go through CDC.
    ///
    /// Requires [`Self::enabled`]. Small objects stay whole-object even when
    /// the feature flag is on.
    pub fn should_chunk(&self, logical_size: u64) -> bool {
        self.enabled && logical_size >= self.min_object_size
    }

    /// `min_size <= avg_size <= max_size`, all within FastCDC v2020 limits.
    pub fn validate(&self) -> Result<(), AppError> {
        use fastcdc::v2020::{AVERAGE_MIN, MAXIMUM_MAX, MAXIMUM_MIN, MINIMUM_MIN};

        if !(self.min_size <= self.avg_size && self.avg_size <= self.max_size) {
            return Err(AppError::InvalidRequest(format!(
                "CDC requires min_size <= avg_size <= max_size, got min={} avg={} max={}",
                self.min_size, self.avg_size, self.max_size
            )));
        }
        if self.min_size < MINIMUM_MIN {
            return Err(AppError::InvalidRequest(format!(
                "CDC min_size must be >= {MINIMUM_MIN} (FastCDC v2020), got {}",
                self.min_size
            )));
        }
        if self.avg_size < AVERAGE_MIN {
            return Err(AppError::InvalidRequest(format!(
                "CDC avg_size must be >= {AVERAGE_MIN} (FastCDC v2020), got {}",
                self.avg_size
            )));
        }
        if self.max_size < MAXIMUM_MIN || self.max_size > MAXIMUM_MAX {
            return Err(AppError::InvalidRequest(format!(
                "CDC max_size must be in {MAXIMUM_MIN}..={MAXIMUM_MAX} (FastCDC v2020), got {}",
                self.max_size
            )));
        }
        Ok(())
    }
}

/// Incremental content-defined chunker backed by FastCDC (Gear hash).
///
/// Feed network/`Bytes` frames with [`push`](Self::push); each call may yield
/// zero or more completed plaintext chunks. Call [`finish`](Self::finish) at
/// EOF to flush the tail.
///
/// HTTP body frames are **not** CDC chunks — concatenate logically, cut with
/// FastCDC.
pub struct CdcChunker {
    config: CdcConfig,
    /// Bytes not yet emitted as a completed chunk.
    pending: Vec<u8>,
}

impl CdcChunker {
    /// Build a chunker. Validates `min <= avg <= max`.
    pub fn new(config: CdcConfig) -> Result<Self, AppError> {
        config.validate()?;
        Ok(Self {
            config,
            pending: Vec::new(),
        })
    }

    pub fn config(&self) -> &CdcConfig {
        &self.config
    }

    /// Absorb `data` and return any chunks that completed on a content boundary
    /// (or hit `max_size`).
    pub fn push(&mut self, data: &[u8]) -> Result<Vec<Vec<u8>>, AppError> {
        if data.is_empty() {
            return Ok(Vec::new());
        }
        self.pending.extend_from_slice(data);
        self.drain_complete_chunks(false)
    }

    /// Flush the final (possibly short) chunk after EOF.
    pub fn finish(mut self) -> Result<Vec<Vec<u8>>, AppError> {
        self.drain_complete_chunks(true)
    }

    /// Emit every chunk FastCDC is willing to finalize.
    ///
    /// When `eof` is false, a cut that would consume the entire buffer is held
    /// back unless the buffer is already `>= max_size` (the algorithm will force
    /// a cut by then). When `eof` is true, the remainder is always emitted.
    fn drain_complete_chunks(&mut self, eof: bool) -> Result<Vec<Vec<u8>>, AppError> {
        let mut out = Vec::new();
        let min = self.config.min_size;
        let avg = self.config.avg_size;
        let max = self.config.max_size;

        loop {
            let len = self.pending.len();
            if len == 0 || (!eof && len < min) {
                break;
            }

            let chunker = FastCDC::new(&self.pending, min, avg, max);
            let (_, end) = chunker.cut(0, len);
            if end == 0 || end > len {
                return Err(AppError::Other(anyhow::anyhow!(
                    "FastCDC returned invalid cut end={end} for pending_len={len}"
                )));
            }

            if !(end < len || len >= max || eof) {
                // Cut ate the whole buffer but we may still receive more data
                // that would move the boundary — hold it.
                break;
            }

            let chunk: Vec<u8> = self.pending.drain(..end).collect();
            out.push(chunk);
        }

        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// FastCDC v2020 floor sizes (see `MINIMUM_MIN` / `AVERAGE_MIN` / `MAXIMUM_MIN`).
    fn tiny_config() -> CdcConfig {
        CdcConfig {
            enabled: true,
            min_size: 64,
            avg_size: 256,
            max_size: 1024,
            min_object_size: 0,
        }
    }

    fn chunk_once(config: CdcConfig, data: &[u8]) -> Result<Vec<Vec<u8>>, AppError> {
        let mut chunker = CdcChunker::new(config)?;
        let mut chunks = chunker.push(data)?;
        chunks.extend(chunker.finish()?);
        Ok(chunks)
    }

    #[test]
    fn default_config_has_sane_ordering() {
        let c = CdcConfig::default();
        assert!(c.min_size <= c.avg_size);
        assert!(c.avg_size <= c.max_size);
        assert!(c.min_object_size >= c.avg_size as u64);
        c.validate().expect("defaults valid");
    }

    #[test]
    fn should_chunk_requires_enabled_and_min_object_size() {
        let off = CdcConfig {
            min_object_size: 1000,
            ..CdcConfig::default()
        };
        assert!(!off.should_chunk(1000));

        let on = CdcConfig {
            enabled: true,
            min_object_size: 1000,
            ..CdcConfig::default()
        };
        assert!(!on.should_chunk(999));
        assert!(on.should_chunk(1000));
    }

    #[test]
    fn default_is_disabled() {
        assert!(!CdcConfig::default().enabled);
    }

    #[test]
    fn validate_rejects_inverted_sizes() {
        let bad = CdcConfig {
            min_size: 100,
            avg_size: 50,
            max_size: 200,
            ..CdcConfig::default()
        };
        assert!(bad.validate().is_err());
    }

    #[test]
    fn validate_rejects_below_fastcdc_floors() {
        // FastCDC v2020: min≥64, avg≥256, max≥1024.
        let too_small_avg = CdcConfig {
            min_size: 64,
            avg_size: 128,
            max_size: 1024,
            ..CdcConfig::default()
        };
        assert!(too_small_avg.validate().is_err());

        let too_small_max = CdcConfig {
            min_size: 64,
            avg_size: 256,
            max_size: 512,
            ..CdcConfig::default()
        };
        assert!(too_small_max.validate().is_err());
    }

    #[test]
    fn new_rejects_invalid_config() {
        let bad = CdcConfig {
            min_size: 64,
            avg_size: 128,
            max_size: 1024,
            ..CdcConfig::default()
        };
        assert!(CdcChunker::new(bad).is_err());
    }

    #[test]
    fn empty_input_yields_no_chunks() {
        let chunks = chunk_once(tiny_config(), b"").expect("chunk");
        assert!(chunks.is_empty());
    }

    #[test]
    fn push_empty_slice_is_noop() {
        let mut chunker = CdcChunker::new(tiny_config()).expect("new");
        assert!(chunker.push(b"").expect("push").is_empty());
        assert!(chunker.finish().expect("finish").is_empty());
    }

    #[test]
    fn push_holds_bytes_below_min_until_finish() {
        let mut chunker = CdcChunker::new(tiny_config()).expect("new");
        let data = vec![b'x'; 40]; // below min_size (64)
        assert!(
            chunker.push(&data).expect("push").is_empty(),
            "must not emit a non-final chunk shorter than min_size"
        );
        let finished = chunker.finish().expect("finish");
        assert_eq!(finished.len(), 1);
        assert_eq!(finished[0], data);
    }

    #[test]
    fn small_buffer_is_one_chunk_on_finish() {
        let data = vec![b'x'; 40]; // below min_size
        let chunks = chunk_once(tiny_config(), &data).expect("chunk");
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0], data);
    }

    #[test]
    fn config_accessor_returns_construction_config() {
        let cfg = tiny_config();
        let chunker = CdcChunker::new(cfg).expect("new");
        assert_eq!(chunker.config(), &cfg);
    }

    #[test]
    fn push_then_finish_reassembles() {
        let data: Vec<u8> = (0..2000u32).map(|i| (i % 251) as u8).collect();
        let mut chunker = CdcChunker::new(tiny_config()).expect("new");
        let mut parts = Vec::new();
        // Feed in awkward network-sized frames.
        for frame in data.chunks(37) {
            parts.extend(chunker.push(frame).expect("push"));
        }
        parts.extend(chunker.finish().expect("finish"));

        let rejoined: Vec<u8> = parts.into_iter().flatten().collect();
        assert_eq!(rejoined, data);
    }

    #[test]
    fn insert_preserves_later_chunk_digests() {
        // The whole point of CDC: a front insert should not rewrite every chunk.
        use sha2::{Digest as _, Sha256};

        let cfg = tiny_config();
        // Several avg-sized regions so an insert at the front leaves a tail to share.
        let base: Vec<u8> = (0..16_384u32).map(|i| (i % 251) as u8).collect();
        let mut edited = Vec::with_capacity(base.len() + 1);
        edited.push(0xFF);
        edited.extend_from_slice(&base);

        let chunks_a = chunk_once(cfg, &base).expect("a");
        let chunks_b = chunk_once(cfg, &edited).expect("b");

        let digests_a: Vec<_> = chunks_a
            .iter()
            .map(|c| Sha256::digest(c).to_vec())
            .collect();
        let digests_b: Vec<_> = chunks_b
            .iter()
            .map(|c| Sha256::digest(c).to_vec())
            .collect();

        let shared = digests_a.iter().filter(|d| digests_b.contains(d)).count();
        assert!(
            shared >= 1,
            "expected some shared chunk digests after a 1-byte insert; \
             a={} chunks b={} chunks shared={shared}",
            digests_a.len(),
            digests_b.len()
        );
        assert_ne!(digests_a, digests_b);
    }

    #[test]
    fn chunks_respect_max_size() {
        let cfg = tiny_config();
        // Highly compressible / low-entropy may avoid natural cuts — max must bind.
        let data = vec![0u8; cfg.max_size * 4];
        let chunks = chunk_once(cfg, &data).expect("chunk");
        assert!(chunks.len() >= 4);
        for (i, c) in chunks.iter().enumerate() {
            assert!(
                c.len() <= cfg.max_size,
                "chunk {i} len {} > max {}",
                c.len(),
                cfg.max_size
            );
            if i + 1 < chunks.len() {
                assert!(
                    c.len() >= cfg.min_size,
                    "non-final chunk {i} len {} < min {}",
                    c.len(),
                    cfg.min_size
                );
            }
        }
    }

    #[test]
    fn deterministic_cuts_for_same_input() {
        let data: Vec<u8> = (0..8192u32).map(|i| (i * 13 % 256) as u8).collect();
        let a = chunk_once(tiny_config(), &data).expect("a");
        let b = chunk_once(tiny_config(), &data).expect("b");
        assert_eq!(a, b, "FastCDC cuts must be deterministic");
    }
}
