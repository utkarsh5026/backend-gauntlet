//! V1 — The ISO-BMFF demuxer: read the MP4 container by hand.
//!
//! An MP4 is a tree of length-prefixed **boxes** (a.k.a. atoms): each is
//! `[size u32][type u32(fourcc)][payload…]` (with a 64-bit size escape when
//! `size == 1`). The parts we care about:
//!   `ftyp`                                  — brand / compatibility.
//!   `moov` → `trak` → `mdia` → `minf` → `stbl`  — per-track metadata, and the
//!   sample tables inside `stbl`:
//!     `stsd`  codec description (e.g. `avc1` → `avcC` with SPS/PPS)
//!     `stts`  decode durations (run-length: count × delta)
//!     `ctts`  composition offsets (pts − dts) for B-frame reordering
//!     `stsc`  sample-to-chunk mapping
//!     `stsz`  sample sizes
//!     `stco` / `co64`  chunk file offsets (32- or 64-bit)
//!     `stss`  sync-sample (keyframe) list — absent ⇒ every sample is a keyframe
//!   `mdat`  — the actual coded media bytes the tables point into.
//!
//! The whole job of V1 is to *cross-reference* those tables into one flat
//! [`Sample`] list per track: for every frame, its byte offset in the file, its
//! size, its decode time, its presentation time, and whether it's a keyframe.
//! Everything downstream (segmenting, manifests) reads that list, never the boxes.
//!
//! `Buf::get_u32`/`get_u64` read **big-endian**, which is exactly the box wire
//! format — so no separate byte-order crate is needed.

use bytes::Bytes;

use crate::error::AppError;

/// Media timescale: ticks per second for a track's timing fields.
pub type Timescale = u32;

/// One coded sample (frame) located in the source file.
#[derive(Debug, Clone)]
pub struct Sample {
    /// Absolute byte offset of this sample's data in the source file.
    pub offset: u64,
    /// Size of the sample in bytes.
    pub size: u32,
    /// Decode timestamp, in the track's timescale (running sum of `stts` deltas).
    pub decode_time: u64,
    /// This sample's decode duration, in the track's timescale (`stts` delta).
    pub duration: u32,
    /// Composition offset (`ctts`): presentation_time = decode_time + this. Zero
    /// when the stream is in decode order (no B-frames / no `ctts`).
    pub composition_offset: i32,
    /// True if this is a sync sample (keyframe / IDR) — a valid segment boundary.
    pub is_sync: bool,
}

/// What kind of media a track carries. Segmenting keys off the video track.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrackKind {
    Video,
    Audio,
    Other,
}

/// Codec setup extracted from `stsd`, kept so V2 can build the init segment's
/// `moov` without re-reading the source.
#[derive(Debug, Clone)]
pub struct CodecConfig {
    /// FourCC of the sample entry (e.g. `avc1`, `hev1`, `mp4a`).
    pub sample_entry: [u8; 4],
    /// The codec-specific configuration box payload (e.g. the `avcC` bytes:
    /// SPS/PPS). Emitted verbatim into the init segment.
    pub setup: Bytes,
    /// Coded picture size for video (0 for audio). Feeds the manifest `RESOLUTION`.
    pub width: u16,
    pub height: u16,
}

/// One demuxed track: its timing base, kind, codec setup, and flat sample list.
#[derive(Debug, Clone)]
pub struct Track {
    pub id: u32,
    pub timescale: Timescale,
    pub kind: TrackKind,
    pub codec: CodecConfig,
    /// Every sample in decode order. The heart of V1.
    pub samples: Vec<Sample>,
}

impl Track {
    /// Total decode duration of the track, in its own timescale.
    pub fn duration(&self) -> u64 {
        self.samples.iter().map(|s| s.duration as u64).sum()
    }
}

/// The whole demuxed asset: the source's tracks. Owned (no borrow of the source
/// bytes), so it can be cached independently of the file buffer.
#[derive(Debug, Clone)]
pub struct MediaInfo {
    pub tracks: Vec<Track>,
}

impl MediaInfo {
    /// The primary video track — what the segmenter and manifests are built from.
    /// (Muxing audio into its own rendition is a stretch; V-path is video-first.)
    pub fn primary_video(&self) -> Result<&Track, AppError> {
        self.tracks
            .iter()
            .find(|t| t.kind == TrackKind::Video)
            .ok_or_else(|| AppError::MalformedMedia("source has no video track".into()))
    }
}

/// Parse a source MP4 into per-track sample tables — the entire V1 deliverable.
///
/// TODO(V1): walk the box tree and build [`MediaInfo`]:
///   1. Iterate top-level boxes; find `moov` (skip `ftyp`/`free`/`mdat` payloads —
///      but remember `mdat`'s file offset, since chunk offsets are absolute).
///   2. For each `trak` under `moov`, descend `mdia/minf/stbl` and read the sample
///      tables. `stts`/`ctts`/`stsc` are run-length encoded — expand them.
///   3. Resolve each sample's **absolute file offset** by combining `stsc`
///      (which chunk a sample is in), the chunk offset (`stco`/`co64`), and the
///      running sum of prior sample sizes (`stsz`) within that chunk.
///   4. Mark sync samples from `stss` (or, if `stss` is absent, mark them all).
///   5. Pull `timescale` from `mdhd`, the kind from `hdlr`, and the codec setup
///      (`avcC`/…) + width/height from `stsd`/`tkhd`.
/// Guardrails: validate every box length against the remaining buffer BEFORE
/// slicing — a size that runs past the end is `AppError::MalformedMedia`, never a
/// panic or an out-of-bounds slice. That robustness is a graded criterion.
pub fn demux(data: &[u8]) -> Result<MediaInfo, AppError> {
    let _ = data;
    todo!("V1: walk the ISO-BMFF box tree and build per-track sample tables")
}

/// Read one box header at `data[pos..]`: returns `(fourcc, payload_range)` and the
/// position just past this box. Plumbing you'll lean on in `demux` — a box is
/// `[size u32][type u32][payload]`, with `size == 1` meaning a 64-bit size follows
/// the type, and `size == 0` meaning "to end of file".
///
/// TODO(V1): implement the length/overflow checks (this is where a malformed file
/// must turn into an error, not a panic). Left unimplemented so the bounds rules
/// are yours to get right.
#[allow(dead_code)]
pub(crate) fn read_box_header(data: &[u8], pos: usize) -> Result<BoxHeader, AppError> {
    let _ = (data, pos);
    todo!("V1: parse a box header with 32/64-bit size + bounds checks")
}

/// A parsed box header: its type and where its payload lives in the buffer.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub(crate) struct BoxHeader {
    pub fourcc: [u8; 4],
    /// Byte range of the payload (after the header) within the source buffer.
    pub payload: std::ops::Range<usize>,
    /// Offset just past the whole box — where the next sibling starts.
    pub end: usize,
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the demuxer against a small committed fixture MP4:
    //   - the sample count and summed duration equal the source's (ffprobe them);
    //   - the sync-sample offsets match the known keyframe positions;
    //   - a file using `co64` (64-bit offsets) parses the same as one using `stco`;
    //   - `ctts` is applied: a B-frame stream's presentation order != decode order;
    //   - truncating the file at every length, and flipping random bytes, only ever
    //     returns Err — never panics (property test `malformed_input_never_panics`).
}
