//! V4 — Byte-range delivery: seek and adapt over HTTP.
//!
//! Video seek and single-file packaging both ride on HTTP **`Range`** requests: the
//! player asks for `bytes=a-b` and expects `206 Partial Content` with a
//! `Content-Range` and *only* that slice. This module turns a built resource (an
//! init segment or a media segment, already a `Bytes` in memory) plus an optional
//! `Range` header into the right response.
//!
//! The ABR half of V4 lives in the manifest (the master playlist's rendition ladder,
//! `manifest::hls_master_playlist`) and in the segmenter (aligned boundaries) — this
//! module is the transport: the `206`/`416`/`Content-Range` mechanics every media
//! response needs.

use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;

/// How a `Range` header resolved against a resource of a known length.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Resolved {
    /// No (or `bytes=0-`) range — serve the whole resource as `200`.
    Full,
    /// A satisfiable range: inclusive byte bounds `[start, end]` → `206`.
    Partial { start: u64, end: u64 },
    /// The range can't be satisfied (start past EOF, reversed) → `416`.
    Unsatisfiable,
}

/// Interpret a `Range` header value against a resource of `total` bytes (V4 core).
///
/// TODO(V4): parse RFC 7233 single-range syntax and resolve it:
///   - `None` (no header)  → `Resolved::Full`.
///   - `bytes=a-b`         → clamp `b` to `total-1`; if `a > b` or `a >= total`,
///                           `Unsatisfiable`, else `Partial { a, b }`.
///   - `bytes=a-`          → `Partial { a, total-1 }` (open-ended: a → EOF).
///   - `bytes=-n`          → last `n` bytes: `Partial { total-n, total-1 }`
///                           (clamp `n` to `total`).
///   - anything malformed, `total == 0`, or a multi-range (`a-b,c-d`) → decide and
///     document (a simple, correct choice is `Unsatisfiable` for the empty resource
///     and `Full`/`400` for multi-range — pick and note it).
/// Only *single* ranges are required. Return the resolved form; the response
/// (status, headers, slice) is assembled in [`serve_ranged`].
pub fn resolve_range(range_header: Option<&str>, total: u64) -> Resolved {
    let _ = (range_header, total);
    todo!("V4: parse `bytes=` (a-b / a- / -n) against total and resolve it")
}

/// Serve `body` as a media response, honoring an optional `Range` header (V4).
///
/// TODO(V4): build the response from `resolve_range(range_header, body.len())`:
///   - `Full`        → `200`, headers: `Accept-Ranges: bytes`, `Content-Type`,
///                     `Content-Length: total`, a long-lived immutable
///                     `Cache-Control`, and a stable `ETag`; body = the whole thing.
///   - `Partial{s,e}`→ `206`, add `Content-Range: bytes s-e/total` and
///                     `Content-Length: e-s+1`; body = `body.slice(s..=e)`.
///   - `Unsatisfiable`→ `416`, `Content-Range: bytes */total`, empty body.
/// Slicing `Bytes` is O(1) and shares the buffer — no copy, memory bounded by the
/// segment. (Streaming a slice chunk-by-chunk is the horizontal refinement; an
/// in-memory `Bytes::slice` already keeps a *single* segment's worth, which is the
/// bound that matters.)
pub fn serve_ranged(
    body: Bytes,
    content_type: &'static str,
    range_header: Option<&str>,
) -> Response {
    // Threaded so `resolve_range` is exercised first — that is what a real request
    // hits until V4 is implemented.
    let _resolved = resolve_range(range_header, body.len() as u64);
    let _ = (body, content_type);
    todo!("V4: turn the resolved range into a 200 / 206 / 416 response")
}

/// Build a plain `200 OK` response for a text resource (playlists/MPD). Plumbing —
/// no `Range` involved. Media goes through [`serve_ranged`] instead.
pub fn text_response(body: String, content_type: &'static str) -> Response {
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, content_type),
            // VOD playlists are safe to cache briefly; segments/init are immutable
            // (that header is set in `serve_ranged`, TODO(V4)).
            (header::CACHE_CONTROL, "public, max-age=6"),
        ],
        body,
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove the range mechanics (unit-test `resolve_range`, integration-
    // test `serve_ranged`):
    //   - `bytes=0-99` on a 1000-byte body → Partial{0,99}; response is 206 with
    //     `Content-Range: bytes 0-99/1000` and a 100-byte body
    //     (`range_request_returns_206_slice`);
    //   - `bytes=500-`  → Partial{500,999}; `bytes=-100` → Partial{900,999};
    //   - `bytes=2000-` (past EOF) → Unsatisfiable → 416 with `Content-Range: bytes */1000`
    //     (`unsatisfiable_range_returns_416`);
    //   - no header → Full → 200 with `Accept-Ranges: bytes` and full body.
}
