//! V3 — Live fMP4 repackaging: rewrap H.264/AAC into CMAF, no re-encode.
//!
//! The codecs arriving over RTMP (H.264 in AVCC, AAC) are already `<video>`-playable —
//! the job is not to transcode them but to **remux**: rebuild a CMAF **init segment**
//! (`ftyp` + `moov` carrying the codec setup, zero samples) and a running series of
//! **fragments** (`moof` + `mdat`) on a monotonic MP4 timeline, cutting **parts**
//! (~200 ms) on demand and **segments** on IDR keyframes. This overlaps project 11's
//! `segment.rs` box-writing — reuse it. What's new is doing it **live**: the timeline
//! comes from RTMP message timestamps (32-bit, wrapping), not a finished sample table,
//! and you only ever hold the current fragment's samples (the memory bound).

use bytes::Bytes;

use crate::error::AppError;

/// Codec setup extracted from the first RTMP audio/video tags (V2), enough to write an
/// init segment. The AVC decoder config is the `avcC` (SPS/PPS); the AAC config is the
/// AudioSpecificConfig. This is the *setup*, carried in `moov` — not per-frame media.
#[derive(Debug, Clone)]
pub struct CodecConfig {
    /// The `avcC` box payload (SPS/PPS), as delivered in the AVC sequence header.
    pub avc_decoder_config: Bytes,
    /// The AAC AudioSpecificConfig, as delivered in the AAC sequence header.
    pub aac_audio_specific_config: Bytes,
    pub width: u16,
    pub height: u16,
    /// MP4 movie timescale (e.g. 90_000 for video). RTMP ms timestamps map into this.
    pub timescale: u32,
}

/// One access unit (a coded video frame or audio frame) to place in a fragment.
#[derive(Debug, Clone)]
pub struct Sample {
    /// The elementary bytes: length-prefixed NALUs (video) or an AAC frame (audio).
    pub data: Bytes,
    /// Decode timestamp in the movie `timescale` (derived from the RTMP timestamp,
    /// rebased so the session starts near 0 and never runs backwards).
    pub dts: u64,
    /// Presentation timestamp in `timescale` (`dts` + composition offset for B-frames).
    pub pts: u64,
    /// Sample duration in `timescale`.
    pub duration: u32,
    /// True for an IDR keyframe — a segment may only *start* on one of these.
    pub keyframe: bool,
}

/// Build the CMAF **init segment** (`ftyp` + `moov`) from the codec config (V3).
///
/// TODO(V3): write the boxes — `ftyp` (major brand `cmfc`/`iso6`), then `moov` with a
/// `mvhd`, a video `trak` (whose `stsd`→`avc1`→`avcC` carries `avc_decoder_config`,
/// with `width`/`height`), an audio `trak` (`stsd`→`mp4a`→`esds` from the AAC config),
/// and a `mvex`→`trex` per track (declaring these tracks are fragmented). Zero samples
/// live here. It must be **byte-stable** for a given config — that's the caching
/// contract. Reuse project 11's box writer.
pub fn build_init(cfg: &CodecConfig) -> Result<Bytes, AppError> {
    let _ = cfg;
    todo!("V3: build the CMAF init segment (ftyp + moov with avcC/esds, no samples)")
}

/// A stateful live fragmenter: samples are pushed as they arrive off the RTMP session,
/// and a **part** (`moof`+`mdat`) is cut on demand once ~`part_secs` has accumulated,
/// a **segment** boundary being a part that starts on a keyframe.
///
/// The timeline (`baseMediaDecodeTime` in each `tfdt`) must advance monotonically
/// across the whole live session — that is what makes the parts glue seamlessly.
pub struct Fragmenter {
    cfg: CodecConfig,
    /// Samples accumulated since the last cut (the only media held in memory).
    pending: Vec<Sample>,
    /// The decode time of the next fragment's first sample — the running timeline
    /// anchor written into `tfdt`.
    base_decode_time: u64,
}

impl Fragmenter {
    pub fn new(cfg: CodecConfig) -> Self {
        Self {
            cfg,
            pending: Vec::new(),
            base_decode_time: 0,
        }
    }

    /// Buffer one access unit for the fragment currently forming.
    pub fn push(&mut self, sample: Sample) {
        self.pending.push(sample);
    }

    /// The codec config this fragmenter was built with (for `build_init`).
    pub fn config(&self) -> &CodecConfig {
        &self.cfg
    }

    /// Cut the buffered samples into one fragment (`moof`+`mdat`) — an LL-HLS part (V3).
    ///
    /// TODO(V3): write a `moof` (`mfhd` sequence number, one `traf` per track with
    /// `tfhd`, a `tfdt` whose `baseMediaDecodeTime` == `self.base_decode_time`, and a
    /// `trun` listing each sample's size/duration/flags/composition-offset) followed by
    /// an `mdat` holding the sample bytes in order. Then advance `base_decode_time` by
    /// the fragment's total duration and clear `pending`. The returned `Bytes` is what
    /// the live window hands to every viewer — build it once. Keep A/V in sync and the
    /// timeline monotonic across cuts.
    pub fn cut_part(&mut self) -> Result<Bytes, AppError> {
        // `base_decode_time` and `pending` are the live-timeline state a real cut
        // consumes and advances; threaded so the intent is unmistakable.
        let _ = (&self.pending, self.base_decode_time);
        todo!("V3: cut the buffered samples into one moof+mdat fragment (a part)")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V3): prove the packager:
    //   - `build_init` is byte-stable for a fixed config and `ffprobe`-parseable;
    //   - feeding captured access units and concatenating `init + parts` decodes with
    //     monotonic, gapless PTS (`fragments_decode_and_are_gapless`);
    //   - `baseMediaDecodeTime` advances by exactly each fragment's duration, never
    //     backwards, across many cuts (`baseMediaDecodeTime_is_monotonic`);
    //   - a long synthetic stream never grows `pending` without bound (the window /
    //     cut cadence keeps memory flat — `window_bounds_memory`).
}
