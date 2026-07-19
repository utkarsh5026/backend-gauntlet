//! A synthetic constant-bitrate media source — **fully wired**, not a vertical.
//!
//! It emits fixed-size "access units" at a frame rate, a keyframe every GOP, with a 90 kHz
//! RTP timestamp advancing per frame. It is deliberately *not* a real encoder: it produces
//! byte buffers shaped like access units so the sender pipeline (packetize → pace → send)
//! has something to carry before you point a real `ffmpeg -f rtp` feed or camera at it.
//!
//! This is the moral equivalent of project 13's wired `LiveRegistry`: plumbing the vertical
//! work plugs into, given to you so the scaffold runs end-to-end once you fill the todos.

use std::time::Duration;

use bytes::Bytes;
use tokio::time::{interval, Interval, MissedTickBehavior};

use crate::rtp::H264_CLOCK_RATE;

/// One synthetic access unit handed to the packetizer.
pub struct Frame {
    /// Encoded bytes (here: filler sized to hit the target bitrate).
    pub data: Bytes,
    /// Whether this frame is a keyframe (GOP boundary).
    pub keyframe: bool,
    /// 90 kHz RTP timestamp for this frame.
    pub rtp_timestamp: u32,
}

/// A CBR frame generator paced by a tokio interval.
pub struct SyntheticSource {
    ticker: Interval,
    frame_bytes: usize,
    ticks_per_frame: u32,
    gop: u32,
    frame_index: u64,
}

impl SyntheticSource {
    /// `fps` frames/sec, `kbps` target video rate, keyframe every `gop` frames.
    pub fn new(fps: u32, kbps: u32, gop: u32) -> Self {
        let fps = fps.max(1);
        let gop = gop.max(1);
        let mut ticker = interval(Duration::from_micros(1_000_000 / fps as u64));
        // Under load we'd rather skip a tick than try to catch up in a burst.
        ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);
        // bytes/frame = (kbps * 1000 / 8) / fps.
        let frame_bytes = ((kbps.max(1) as usize) * 1000 / 8) / fps as usize;
        Self {
            ticker,
            frame_bytes: frame_bytes.max(1),
            ticks_per_frame: H264_CLOCK_RATE / fps,
            gop,
            frame_index: 0,
        }
    }

    /// Await the next frame's tick and produce it.
    pub async fn next_frame(&mut self) -> Frame {
        self.ticker.tick().await;
        let keyframe = self.frame_index.is_multiple_of(self.gop as u64);
        let rtp_timestamp = (self.frame_index.wrapping_mul(self.ticks_per_frame as u64)) as u32;
        let frame = Frame {
            data: Bytes::from(vec![0u8; self.frame_bytes]),
            keyframe,
            rtp_timestamp,
        };
        self.frame_index = self.frame_index.wrapping_add(1);
        frame
    }
}
