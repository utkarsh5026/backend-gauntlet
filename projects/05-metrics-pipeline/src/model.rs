//! Shared time-series types: the data model the whole pipeline is keyed on.
//!
//! These are the values the verticals pass around — `parse` turns a wire line
//! into [`MetricPoint`]s (V1), `rollup` folds points into [`Aggregate`]s per
//! [`WindowKey`] (V2), the sink writes [`RollupRow`]s (V3), and the SSE feed
//! streams them (V4). The types are deliberately plain data; the *interesting*
//! operations on them (the series fingerprint, the online aggregation, the
//! percentile sketch) live in the vertical modules, not here.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Stable identity of a series = a fingerprint of its measurement + tag set.
///
/// Two points belong to the same series iff their measurement and their *exact*
/// set of tags match. The fingerprint must be computed over the tags **sorted by
/// key** so that `a=1,b=2` and `b=2,a=1` collapse to one series — see
/// [`crate::parse`] (V1) for where this is derived.
pub type SeriesId = u64;

/// A measurement plus its tag set — the dimensions you filter and group by.
///
/// Invariant the parser must uphold: `tags` is sorted by key (canonical form),
/// so [`SeriesId`] is stable regardless of the order tags arrived in.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Series {
    /// What is being measured, e.g. `cpu`, `http_requests`.
    pub measurement: String,
    /// `(key, value)` dimensions, **sorted by key**. Each distinct tag set is a
    /// new series — this is where cardinality comes from (see SPEC V1).
    pub tags: Vec<(String, String)>,
}

/// A single observation: one numeric value for one series at one instant.
///
/// A wire line with several fields (`usage=0.91,sys=0.12`) parses into several
/// `MetricPoint`s — one per field — each its own series (the field name folds
/// into the measurement or a `__field__` tag; that's a V1 modelling choice).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MetricPoint {
    pub series: Series,
    pub value: f64,
    /// When the observation happened. Defaults to ingest time when the wire line
    /// omits a timestamp (a V1 decision).
    pub timestamp: DateTime<Utc>,
}

/// The bucket a point falls into: a series, pinned to a tumbling window start.
///
/// `window_start` is the point's timestamp snapped *down* to a multiple of the
/// window width (so every point in `[start, start + width)` shares one key).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct WindowKey {
    pub series_id: SeriesId,
    pub window_start: DateTime<Utc>,
}

/// The running summary of every point in one window of one series (V2).
///
/// `count/sum/min/max/last` update in a single pass. The percentile sketch is the
/// hard part and is intentionally *not* a field here yet — building a mergeable
/// sketch (histogram / t-digest) and threading it through is the V2 challenge, so
/// it's left for you to add (`p50`/`p99` in [`RollupRow`] are where it surfaces).
#[derive(Debug, Clone, Copy)]
pub struct Aggregate {
    pub count: u64,
    pub sum: f64,
    pub min: f64,
    pub max: f64,
    /// The most recently seen value (by arrival) in the window.
    pub last: f64,
    // TODO(V2): add your mergeable percentile sketch here (e.g. a fixed-bucket
    // histogram or a t-digest) so a window can answer p50/p95/p99 and two windows
    // can be merged for coarser rollups (1m -> 5m -> 1h). You cannot store the
    // percentile itself — percentiles don't average. Store the *distribution*.
}

/// A finished, flushed rollup — what gets written to ClickHouse (V3) and pushed
/// to dashboards over SSE (V4). One row per `(series, window)`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RollupRow {
    pub series_id: SeriesId,
    pub measurement: String,
    /// Start of the window this row summarizes.
    pub window_start: DateTime<Utc>,
    /// Window width in seconds (the rollup resolution: 1, 60, 3600, …).
    pub window_secs: u32,
    pub count: u64,
    pub sum: f64,
    pub min: f64,
    pub max: f64,
    /// Quantiles drawn from the V2 sketch. Zeroed in the scaffold until the
    /// sketch exists — see [`Aggregate`].
    pub p50: f64,
    pub p99: f64,
}

/// The input to `POST /ingest`: a raw line-protocol body.
///
/// It's bytes, not a typed struct, because parsing it *is* V1. The handler hands
/// these bytes to [`crate::parse`]; the wire grammar is yours to define.
#[derive(Debug, Clone)]
pub struct IngestBody {
    pub raw: bytes::Bytes,
}
