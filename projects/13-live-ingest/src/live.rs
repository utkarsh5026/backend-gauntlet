//! The live registry + per-stream window — the shared state between the RTMP
//! producers (publisher sessions) and the HTTP consumers (viewers).
//!
//! This is **plumbing** — it is (mostly) implemented. Each live stream is a bounded
//! ring of built segments/parts plus a `watch` channel that publishes the current
//! **live edge** `(media-sequence, part)`; producers push built fMP4 bytes and bump
//! the edge, consumers read the window and (for LL-HLS blocking reload, V4) *await*
//! the edge. What lands here is already-built `Bytes` — the *building* (V3, `fmp4.rs`)
//! and the *playlist rendering* + blocking policy (V4, `llhls.rs`) are the `todo!()`s;
//! this module just holds and hands out the pieces, memoized so N viewers share one
//! mux (the fan-out contract).
//!
//! A live stream is unbounded in time, so the window is a *fixed* size — RAM tracks
//! `window_segments`, not airtime. That is the whole reason a 10-hour broadcast fits
//! in a few seconds of memory.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use chrono::{DateTime, Utc};
use tokio::sync::watch;

/// Immutable ingest configuration, shared (behind an `Arc`) by every session and
/// handler.
#[derive(Debug, Clone)]
pub struct IngestConfig {
    /// Authorized stream keys. Empty ⇒ allow any key (dev only — see `authorize`).
    pub stream_keys: Vec<String>,
    /// LL-HLS part target in seconds (~200–350 ms).
    pub target_part_secs: f64,
    /// Full-segment target in seconds.
    pub target_segment_secs: f64,
    /// How many finished segments to retain in the live window (the memory bound).
    pub window_segments: usize,
}

/// The live edge: the newest `(media_sequence, part)` a consumer can ask for. LL-HLS
/// blocking reload (V4) waits until this reaches a requested `(msn, part)`.
///
/// Ordered `msn`-major then `part`-minor so `await_edge` is a simple `>=` compare.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, PartialOrd, Ord)]
pub struct LiveEdge {
    pub msn: u64,
    pub part: u64,
}

/// One built partial segment (an LL-HLS **part**, ~200 ms of fMP4).
#[derive(Debug, Clone)]
pub struct Part {
    pub bytes: Bytes,
    pub duration: f64,
    /// True when this part begins on an IDR keyframe (`#EXT-X-PART:…,INDEPENDENT=YES`).
    pub independent: bool,
}

/// One media segment in the live window: a media sequence number, its parts, and —
/// once closed — the concatenated full-segment bytes.
#[derive(Debug, Clone)]
pub struct Segment {
    pub msn: u64,
    pub parts: Vec<Part>,
    /// `false` while still accumulating parts at the live edge.
    pub complete: bool,
    /// Full-segment bytes, present once `complete` (init + all parts is what a
    /// non-LL player fetches).
    pub bytes: Option<Bytes>,
    pub duration: f64,
    /// Wall-clock start, for `#EXT-X-PROGRAM-DATE-TIME`.
    pub program_date_time: DateTime<Utc>,
}

/// One live stream's bounded window of built media plus its edge notifier.
pub struct LiveStream {
    inner: Mutex<StreamInner>,
    edge_tx: watch::Sender<LiveEdge>,
    window_segments: usize,
}

struct StreamInner {
    /// CMAF init segment (`ftyp`+`moov`), built once by V3 when the codec config lands.
    init: Option<Bytes>,
    /// Rolling window of segments; the front is the oldest retained, the back is the
    /// one currently forming at the live edge.
    segments: VecDeque<Segment>,
    /// Media sequence number of the next segment to open (monotonic; never reused).
    next_msn: u64,
    /// True once the publisher has disconnected — the playlist gets `#EXT-X-ENDLIST`.
    ended: bool,
}

impl LiveStream {
    fn new(window_segments: usize) -> Self {
        let (edge_tx, _) = watch::channel(LiveEdge::default());
        Self {
            inner: Mutex::new(StreamInner {
                init: None,
                segments: VecDeque::new(),
                next_msn: 0,
                ended: false,
            }),
            edge_tx,
            window_segments,
        }
    }

    // -- producer side (called from a publisher session, V3) -----------------

    /// Install the CMAF init segment (once, when the codec config is known).
    pub fn set_init(&self, init: Bytes) {
        self.inner.lock().unwrap().init = Some(init);
    }

    /// Append a freshly built part to the segment currently forming at the live edge,
    /// opening a new segment first if `start_segment` is true (a keyframe boundary).
    /// Bumps the live edge, unparking any held blocking-reload requests (V4).
    pub fn push_part(&self, part: Part, start_segment: bool) {
        let edge = {
            let mut g = self.inner.lock().unwrap();
            if start_segment || g.segments.is_empty() {
                let msn = g.next_msn;
                g.next_msn += 1;
                g.segments.push_back(Segment {
                    msn,
                    parts: Vec::new(),
                    complete: false,
                    bytes: None,
                    duration: 0.0,
                    program_date_time: Utc::now(),
                });
                // Trim the window from the front to keep memory bounded.
                while g.segments.len() > self.window_segments {
                    g.segments.pop_front();
                }
            }
            let seg = g.segments.back_mut().expect("segment just ensured");
            seg.duration += part.duration;
            seg.parts.push(part);
            LiveEdge {
                msn: seg.msn,
                part: (seg.parts.len() - 1) as u64,
            }
        };
        // Ignore send errors: no receivers just means no one is blocking right now.
        let _ = self.edge_tx.send(edge);
    }

    /// Close the segment currently at the live edge, recording its full-segment bytes
    /// (init-independent concatenation of its parts, built by V3).
    pub fn finish_segment(&self, full_bytes: Bytes) {
        let mut g = self.inner.lock().unwrap();
        if let Some(seg) = g.segments.back_mut() {
            seg.complete = true;
            seg.bytes = Some(full_bytes);
        }
    }

    /// Mark the stream ended (publisher gone) — the playlist gains `#EXT-X-ENDLIST`.
    pub fn mark_ended(&self) {
        self.inner.lock().unwrap().ended = true;
    }

    // -- consumer side (called from HTTP handlers, V4) -----------------------

    /// The current live edge.
    pub fn edge(&self) -> LiveEdge {
        *self.edge_tx.borrow()
    }

    /// Await the live edge reaching `target` (LL-HLS blocking reload). Returns the
    /// edge once `edge() >= target`, or the last edge if the stream ends first. The
    /// *policy* (what a blocking `_HLS_msn/_HLS_part` maps to, the timeout) is V4 in
    /// `llhls.rs`; this is just the park-until-signalled mechanism it builds on.
    pub async fn await_edge(&self, target: LiveEdge) -> LiveEdge {
        let mut rx = self.edge_tx.subscribe();
        loop {
            let cur = *rx.borrow_and_update();
            if cur >= target {
                return cur;
            }
            if rx.changed().await.is_err() {
                return cur; // producer gone
            }
        }
    }

    pub fn is_ended(&self) -> bool {
        self.inner.lock().unwrap().ended
    }

    /// The init segment, if built yet.
    pub fn init_bytes(&self) -> Option<Bytes> {
        self.inner.lock().unwrap().init.clone()
    }

    /// The full bytes of a completed segment by media sequence number.
    pub fn segment_bytes(&self, msn: u64) -> Option<Bytes> {
        let g = self.inner.lock().unwrap();
        g.segments
            .iter()
            .find(|s| s.msn == msn)
            .and_then(|s| s.bytes.clone())
    }

    /// The bytes of one part `(msn, part)` if still in the window.
    pub fn part_bytes(&self, msn: u64, part: u64) -> Option<Bytes> {
        let g = self.inner.lock().unwrap();
        g.segments
            .iter()
            .find(|s| s.msn == msn)
            .and_then(|s| s.parts.get(part as usize))
            .map(|p| p.bytes.clone())
    }

    /// A cloned snapshot of the window, for rendering the playlist (V4). Cloning is
    /// cheap: `Bytes` clones are refcount bumps, not copies.
    pub fn snapshot(&self) -> (Vec<Segment>, bool) {
        let g = self.inner.lock().unwrap();
        (g.segments.iter().cloned().collect(), g.ended)
    }
}

/// The set of live streams, keyed by stream key. Publisher sessions create entries;
/// viewers look them up.
pub struct LiveRegistry {
    streams: Mutex<HashMap<String, Arc<LiveStream>>>,
    cfg: Arc<IngestConfig>,
}

impl LiveRegistry {
    pub fn new(cfg: Arc<IngestConfig>) -> Self {
        Self {
            streams: Mutex::new(HashMap::new()),
            cfg,
        }
    }

    pub fn config(&self) -> &IngestConfig {
        &self.cfg
    }

    /// Is this key allowed to publish? An empty allow-list means "any key" (dev).
    ///
    /// TODO(security, horizontal): this is the auth gate the SPEC grades. A static
    /// allow-list is the floor; a real deployment verifies a signed token / calls an
    /// auth service here. Never log the key itself — log a hash/prefix.
    pub fn authorize(&self, key: &str) -> bool {
        self.cfg.stream_keys.is_empty() || self.cfg.stream_keys.iter().any(|k| k == key)
    }

    /// Get-or-create the live stream for a key (publisher side).
    pub fn open(&self, key: &str) -> Arc<LiveStream> {
        let mut g = self.streams.lock().unwrap();
        g.entry(key.to_string())
            .or_insert_with(|| Arc::new(LiveStream::new(self.cfg.window_segments)))
            .clone()
    }

    /// Look up a live stream for a key (viewer side); `None` if nothing is on air.
    pub fn get(&self, key: &str) -> Option<Arc<LiveStream>> {
        self.streams.lock().unwrap().get(key).cloned()
    }

    /// Remove a stream when its publisher disconnects.
    pub fn close(&self, key: &str) {
        self.streams.lock().unwrap().remove(key);
    }

    /// Keys currently on air, sorted — for `GET /live`.
    pub fn live_keys(&self) -> Vec<String> {
        let mut keys: Vec<String> = self.streams.lock().unwrap().keys().cloned().collect();
        keys.sort();
        keys
    }
}
