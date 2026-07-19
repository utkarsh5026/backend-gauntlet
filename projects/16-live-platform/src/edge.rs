//! V3 — LL-HLS edge delivery with request coalescing. `src/edge.rs`.
//!
//! Between the packager (origin) and thousands of viewers sits an edge. Its whole job is to
//! serve the *same* freshly-produced bytes to a crowd without melting the origin — and to do
//! it at **low latency**, which is where LL-HLS gets subtle:
//!
//! 1. **Single-flight on a cold segment.** The instant a new partial/segment is referenced by
//!    the playlist, every viewer requests it at once. If the edge doesn't have it yet, exactly
//!    **one** fill should go to origin while the rest *wait on that one fill* — a classic
//!    thundering-herd / cache-stampede, now on video segments. (Same failure the URL shortener's
//!    boss taught, one tier up.)
//! 2. **Blocking playlist reload.** LL-HLS players long-poll the media playlist with
//!    `_HLS_msn`/`_HLS_part`: "hold the request open until media-sequence N part K exists, then
//!    return." That blocking-read is what shaves latency toward glass-to-glass ~2s — done wrong,
//!    it either busy-polls or returns stale.
//!
//! On a miss the edge pulls from the packager origin over HTTP and streams it through as
//! `Bytes` (cheap-to-clone fan-out). Scaffold state: the cache is constructed with an origin
//! base + HTTP client; the serve paths are the V3 `todo!()` worklist.

use std::collections::HashSet;
use std::sync::Mutex;

use bytes::Bytes;

use crate::error::Result;

/// Which part of a media playlist a blocking reload is waiting for. A player asks for
/// "media-sequence `msn`, part `part`" and the edge holds the request until it exists.
#[derive(Clone, Copy, Debug)]
pub struct PlaylistCursor {
    pub msn: u64,
    pub part: Option<u32>,
}

/// The edge cache in front of the packager origin.
pub struct EdgeCache {
    /// Base URL of the packager origin, e.g. `http://packager:9000`. A miss fetches from here.
    origin_base: String,
    /// Pooled HTTP client for origin fills (keep-alive reuse across misses).
    http: reqwest::Client,
    /// Keys currently being filled from origin — the single-flight set. A request whose key
    /// is already in flight waits for that fill instead of starting its own.
    inflight: Mutex<HashSet<String>>,
}

impl EdgeCache {
    /// Build the edge over a packager origin base URL. Wiring only.
    pub fn new(origin_base: String) -> Self {
        Self {
            origin_base,
            http: reqwest::Client::new(),
            inflight: Mutex::new(HashSet::new()),
        }
    }

    pub fn origin_base(&self) -> &str {
        &self.origin_base
    }

    // ---- V3 worklist: blocking reload + single-flight fan-out ---------------------

    /// TODO(V3): Serve the **master** playlist for a stream — the list of ABR renditions a
    /// player picks from. Cacheable and cheap; the interesting latency work is the media
    /// playlist below. Fetch-through from origin on a miss.
    pub async fn master_playlist(&self, stream_key: &str) -> Result<String> {
        let _ = (stream_key, &self.http);
        todo!("V3: return the master m3u8 (fetch-through from origin, then cache)")
    }

    /// TODO(V3): Serve a **media** playlist with LL-HLS blocking reload. If `cursor` names a
    /// part that doesn't exist yet, hold the request open until it's produced (or a deadline),
    /// then return the updated playlist — never busy-poll, never return stale past the cursor.
    pub async fn media_playlist(
        &self,
        stream_key: &str,
        rendition: &str,
        cursor: Option<PlaylistCursor>,
    ) -> Result<String> {
        let _ = (stream_key, rendition, cursor);
        todo!("V3: blocking playlist reload — await the requested msn/part, then serve")
    }

    /// TODO(V3): Serve a segment or partial segment's bytes. On a cold key, use `inflight`
    /// as a single-flight gate so a crowd racing for the same just-produced part triggers
    /// **one** origin fill and the rest await it. Support HTTP byte-range for seeking.
    pub async fn segment(
        &self,
        stream_key: &str,
        rendition: &str,
        name: &str,
        range: Option<(u64, u64)>,
    ) -> Result<Bytes> {
        let _ = (stream_key, rendition, name, range, &self.inflight);
        todo!("V3: single-flight the origin fill on a miss, fan the same Bytes out to all waiters")
    }
}
