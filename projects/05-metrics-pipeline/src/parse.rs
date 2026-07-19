//! V1 — The ingest parser + the series fingerprint.
//!
//! Turn a wire line into typed [`MetricPoint`]s and give every series a stable
//! identity. This is the front door of the pipeline: get the data model wrong
//! here and every downstream stage (rollup, sink, query) inherits the bug.
//!
//! The wire format is a *line protocol* — the InfluxDB shape is a good target:
//!
//! ```text
//! measurement,tag1=v1,tag2=v2 field1=val1,field2=val2 timestamp
//! cpu,host=a,region=us         usage=0.91,sys=0.12     1719600000
//! ```
//!
//! The two traps to internalize (see SPEC V1): **series identity** (the
//! fingerprint must be order-independent, so tags are sorted before hashing) and
//! **cardinality** (the product of distinct tag values — your cost function).

use crate::error::AppError;
use crate::model::{MetricPoint, Series, SeriesId};

/// Compute the stable fingerprint of a series.
///
/// Same measurement + same set of tags ⇒ same id, regardless of the order the
/// tags were written on the wire. That order-independence is the whole point:
/// `a=1,b=2` and `b=2,a=1` are the *same* series and must collide here.
pub fn fingerprint(series: &Series) -> SeriesId {
    // TODO(V1): hash the measurement and the tags into a u64. Notes:
    //   - sort tags by key FIRST (or require the caller to) so the hash is
    //     canonical — this is the load-bearing step.
    //   - use a fast, stable hasher (FNV-1a is a few lines; or xxhash). Avoid
    //     `DefaultHasher`'s randomized seed if you ever persist/compare ids
    //     across processes.
    //   - include a separator between fields so `ab|c` and `a|bc` don't collide.
    let _ = series;
    todo!("V1: fingerprint the measurement + sorted tags into a SeriesId")
}

/// Parse a full line-protocol payload (possibly many lines) into points.
///
/// One wire line with N fields expands to N points (one per field), each its own
/// series. A malformed line is a hard error here — the caller rejects the request
/// and increments a `points_rejected` counter; a bad line must never silently
/// corrupt the batch.
pub fn parse(input: &str) -> Result<Vec<MetricPoint>, AppError> {
    // TODO(V1): parse `input` into points. Suggested shape:
    //   - split on newlines; skip blank lines and `#` comments.
    //   - for each line, split into three space-separated sections:
    //       <measurement,tagset>  <fieldset>  [timestamp]
    //   - parse the tagset into sorted (key,value) pairs -> build a `Series`
    //     (remember to sort by key so `fingerprint` is canonical).
    //   - parse the fieldset (`k=v,k=v`) into one MetricPoint per field; how a
    //     field name maps into the series is YOUR model choice (fold it into the
    //     measurement, or carry it as a reserved tag).
    //   - parse the optional trailing timestamp; default to `Utc::now()` when
    //     absent. Reject an absurd timestamp (far past/future) — that's a V1
    //     validation lesson, and a security one (bad ts -> bad partition).
    //   - VALIDATE + CAP: line length, tag count, key/value charset & length.
    //     An unbounded tag is the cardinality DoS vector (SPEC: security).
    let _ = input;
    todo!("V1: parse line protocol into MetricPoints (and reject malformed lines)")
}

#[cfg(test)]
mod tests {
    // TODO(V1): the parser is pure, so test it hard (no broker/store needed):
    //   - a well-formed multi-field line yields one point per field with the
    //     right value, tags, and timestamp;
    //   - tag order does NOT change the SeriesId (a=1,b=2 == b=2,a=1);
    //   - different tag *values* DO change the SeriesId (host=a != host=b);
    //   - a missing timestamp defaults to ~now; an absurd one is rejected;
    //   - malformed lines (no fields, bad number, oversized) return an error and
    //     don't panic. (`proptest` is a dev-dep — fuzz the line splitter.)
}
