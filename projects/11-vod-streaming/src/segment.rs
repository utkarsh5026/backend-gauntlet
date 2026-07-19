//! V2 — The fMP4 / CMAF segmenter: write the boxes by hand.
//!
//! This turns V1's sample table into a **CMAF init segment** plus a run of
//! **keyframe-aligned media segments** — the marquee vertical.
//!
//! Two outputs:
//!   * **init segment** = `ftyp` + `moov` carrying the codec setup and *zero*
//!     samples. Same for every media segment of a rendition; deterministic.
//!   * **media segment** = `styp` + `moof` + `mdat`. The `moof` (`mfhd` + `traf`
//!     [`tfhd` + `tfdt` + `trun`]) describes the fragment: `tfdt`'s
//!     `baseMediaDecodeTime` anchors it on the timeline, and `trun` lists each
//!     sample's size, duration, and composition offset. The `mdat` is the coded
//!     bytes copied out of the source at the offsets V1 recorded.
//!
//! The rule that makes it work: **every segment starts on a keyframe**. The target
//! duration is a goal, not a law — you accumulate GOPs until the next keyframe would
//! push you past the target, then cut. A segment that can't be decoded standalone is
//! the bug this vertical exists to avoid.

use bytes::Bytes;

use crate::error::AppError;
use crate::isobmff::Track;

/// One planned segment: which samples it covers and its place on the timeline.
/// Computed from the sample table without touching media bytes — cheap enough to
/// (re)compute per request, though V4 memoizes the *built* bytes.
#[derive(Debug, Clone)]
pub struct SegmentEntry {
    /// 0-based segment index — matches the URL `.../seg/{index}` and the manifest.
    pub index: usize,
    /// Decode time of this segment's first sample, in the track timescale. Becomes
    /// the fragment's `tfdt` `baseMediaDecodeTime`; equals the summed durations of
    /// all prior segments.
    pub start_time: u64,
    /// Total decode duration of this segment, in the track timescale. Drives the
    /// manifest `#EXTINF`.
    pub duration: u64,
    /// Half-open range into `Track::samples` this segment covers.
    pub samples: std::ops::Range<usize>,
}

impl SegmentEntry {
    /// Segment duration in seconds, given the track timescale — for the manifest.
    pub fn seconds(&self, timescale: u32) -> f64 {
        self.duration as f64 / timescale as f64
    }
}

/// The full segmentation plan for one rendition: the ordered segment list.
#[derive(Debug, Clone)]
pub struct SegmentIndex {
    pub segments: Vec<SegmentEntry>,
}

impl SegmentIndex {
    /// The longest segment in seconds, rounded up — the HLS `#EXT-X-TARGETDURATION`.
    pub fn target_duration(&self, timescale: u32) -> u32 {
        self.segments
            .iter()
            .map(|s| s.seconds(timescale).ceil() as u32)
            .max()
            .unwrap_or(0)
    }
}

/// Group a track's samples into keyframe-aligned segments of ~`target_secs`.
///
/// TODO(V2): the segmentation policy.
///   - Walk `track.samples`. A new segment may only *begin* on a sync sample
///     (`is_sync`). Accumulate samples; when adding the next GOP (up to the next
///     sync sample) would exceed `target_secs` of accumulated duration, close the
///     current segment there and start the next at that keyframe.
///   - Never split a GOP to hit the target — the keyframe boundary always wins, so
///     real segment lengths cluster around, not exactly on, the target.
///   - Set each `SegmentEntry`'s `start_time` to the running decode time and its
///     `duration` to the sum of its samples' durations. Consecutive segments must
///     be gapless: `start_time[n+1] == start_time[n] + duration[n]`.
///   - If the source has no sync samples flagged, treat every sample as a valid
///     boundary (all-keyframe / intra content).
pub fn plan_segments(track: &Track, target_secs: f64) -> Result<SegmentIndex, AppError> {
    let _ = (track, target_secs);
    todo!("V2: group samples into keyframe-aligned segments of ~target_secs")
}

/// Build the CMAF **init segment** (`ftyp` + `moov`, codec setup, no samples).
///
/// TODO(V2): emit the boxes.
///   - `ftyp`: a major brand + compatible brands announcing fragmented/CMAF.
///   - `moov` = `mvhd` + `trak`(`tkhd` + `mdia`[`mdhd` + `hdlr` + `minf`[…`stbl`
///     with the `stsd`/codec box from `track.codec.setup`, and *empty* sample
///     tables]]) + `mvex`(`trex`) — the `mvex`/`trex` is what declares "samples
///     live in fragments, not here". Zero media samples in the init.
/// The result must be **byte-for-byte identical** across calls for the same track
/// (no timestamps-of-day, no random ids) — that determinism is a graded criterion
/// and what lets V4 cache + `ETag` it.
pub fn build_init_segment(track: &Track) -> Result<Bytes, AppError> {
    let _ = track;
    todo!("V2: emit ftyp + moov (codec config, mvex/trex, no samples)")
}

/// Build one **media segment** (`styp` + `moof` + `mdat`) for `entry`.
///
/// `source` is the full source-file buffer; `entry.samples` indexes `track.samples`,
/// whose `offset`/`size` locate each sample's bytes inside `source`.
///
/// TODO(V2): emit the fragment.
///   - `styp`: segment type box (brands as in `ftyp`).
///   - `moof` = `mfhd`(sequence number = `entry.index + 1`) + `traf`[`tfhd`(track
///     id, default flags) + `tfdt`(`baseMediaDecodeTime = entry.start_time`) +
///     `trun`(per-sample size, duration, and `composition_offset`, with the
///     data-offset pointing at the first byte of `mdat`'s payload)].
///   - `mdat`: the coded bytes — copy `source[s.offset .. s.offset + s.size]` for
///     each sample in `entry.samples`, in order.
///   - The `trun` `data_offset` must equal the distance from the start of the
///     `moof` to the first `mdat` byte — get this wrong and players read garbage.
/// Copy only this segment's bytes: memory stays bounded by segment size, not asset
/// size (a graded criterion).
pub fn build_media_segment(
    source: &[u8],
    track: &Track,
    entry: &SegmentEntry,
) -> Result<Bytes, AppError> {
    let _ = (source, track, entry);
    todo!("V2: emit styp + moof(mfhd/tfhd/tfdt/trun) + mdat for this segment")
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove the segmenter:
    //   - every segment's first sample is a sync sample (keyframe-aligned);
    //   - segments are gapless and their durations sum to the track duration;
    //   - `build_init_segment` returns identical bytes on repeated calls;
    //   - init ++ any one media segment is accepted + decoded by ffprobe/mp4box
    //     (integration/bench: `init_plus_segment_is_decodable`).
}
