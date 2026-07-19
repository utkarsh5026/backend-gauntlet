//! V4 — Server-side recording. `src/recording.rs`.
//!
//! A recorded meeting is not a magic side-channel bolted onto the SFU — the clean design is that
//! the recorder is **another subscriber**: it joins the room like any viewer, receives each
//! publisher's forwarded RTP, and writes it to disk instead of a screen. That framing *is* the
//! learning — recording rides the exact cascade you built (the recorder subscribes in some region
//! and pulls a remote publisher over a relay leg if needed, counting as demand for it) and inherits
//! the SFU's **no-transcode** property (you persist the encoded RTP, you don't re-encode a pixel).
//!
//! The parts genuinely the recorder's own: **cross-track wall-clock alignment** — each publisher's
//! RTP timestamp is on its own clock with an arbitrary offset, so to lay N tracks on one timeline
//! you map each track's RTP-ts → wall clock using its **RTCP sender reports** (the SR carries the
//! RTP-ts ↔ NTP-time correspondence — the project 14 clock-mapping idea). And **durable, segmented
//! output** — written in segments with an index, so a crash loses at most the open segment, and
//! `stop`/shutdown finalizes cleanly.
//!
//! Scaffold state: [`Recorder::new`] and the read-only [`active`](Recorder::active) snapshot are
//! wired so `/status` shows the (empty) recording table. Starting/stopping a recording, writing
//! packets, aligning via SR, and finalizing are the V4 `todo!()` worklist.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

use serde::Serialize;

use crate::error::Result;

/// A live recording of one room: the recorder-as-subscriber's bookkeeping. One per recorded room.
#[derive(Clone, Debug, Serialize)]
pub struct ActiveRecording {
    pub room_id: String,
    /// Unix-millis the recording started (the timeline origin for wall-clock alignment).
    pub started_at_ms: i64,
    /// How many publisher tracks this recording is subscribed to.
    pub tracks: usize,
    /// Segments finalized (closed + indexed) so far.
    pub segments: usize,
}

/// Config for the recorder, read from env in `main`.
pub struct RecordingConfig {
    /// Where segmented recordings are written (one subtree per room).
    pub dir: PathBuf,
    /// Segment length in seconds — a crash loses at most one open segment.
    pub segment_secs: f64,
}

/// Mutable recording table, guarded by one lock.
#[derive(Default)]
struct Inner {
    /// Active recordings keyed by room id.
    recordings: HashMap<String, ActiveRecording>,
}

/// The server-side recorder: a set of recordings, each a durable subscriber to a room.
pub struct Recorder {
    cfg: RecordingConfig,
    inner: Mutex<Inner>,
}

impl Recorder {
    /// Build the recorder. Wiring only — no filesystem I/O until a recording starts.
    pub fn new(cfg: RecordingConfig) -> Self {
        Self {
            cfg,
            inner: Mutex::new(Inner::default()),
        }
    }

    pub fn config(&self) -> &RecordingConfig {
        &self.cfg
    }

    /// A snapshot of active recordings (for `/status`).
    pub fn active(&self) -> Vec<ActiveRecording> {
        self.inner
            .lock()
            .expect("recorder lock")
            .recordings
            .values()
            .cloned()
            .collect()
    }

    // ---- V4 worklist: subscribe · write · align · finalize --------------------------------

    /// TODO(V4): Start recording `room_id` by subscribing the recorder to **every** publisher in
    /// the room (pulling remote publishers over a cascade leg if they're in another region — the
    /// recorder counts as demand for those streams). Create the room's output subtree + first
    /// segment. **Idempotent**: a second start for an already-recording room is a no-op (no fork).
    pub async fn start(&self, room_id: &str) -> Result<()> {
        let _ = (room_id, &self.cfg.dir);
        todo!("V4: subscribe the recorder to all publishers, open the first segment")
    }

    /// TODO(V4): Stop recording `room_id`: finalize + flush the open segment, close the index.
    /// **Idempotent** (safe on an unknown / already-stopped room) — also called on graceful
    /// shutdown so a cleanly-stopped recording always has a complete, closed index.
    pub async fn stop(&self, room_id: &str) -> Result<()> {
        let _ = room_id;
        todo!("V4: finalize open segments, close the index, mark the recording stopped")
    }

    /// TODO(V4): Append one track's encoded RTP `packet` to its segment (byte-identical — no
    /// transcode), rolling to a new segment every `segment_secs`. `rtcp_sr` (when present) carries
    /// the RTP-ts ↔ NTP-time correspondence used to place this track on the room's common wall
    /// clock (project 14's SR-based clock mapping).
    pub async fn on_track_packet(
        &self,
        room_id: &str,
        publisher: u64,
        packet: &[u8],
        rtcp_sr: Option<&[u8]>,
    ) -> Result<()> {
        let _ = (room_id, publisher, packet, rtcp_sr, self.cfg.segment_secs);
        todo!("V4: append encoded RTP to the segment, roll segments, track SR wall-clock mapping")
    }

    /// TODO(V4): Flush + finalize **all** open recordings on graceful shutdown, so no recording is
    /// left with a truncated open segment or an unclosed index.
    pub async fn flush_all(&self) -> Result<()> {
        todo!("V4: finalize every active recording (called on SIGTERM)")
    }
}
