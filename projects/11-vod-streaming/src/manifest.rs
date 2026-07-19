//! V3 — Manifest generation: HLS `.m3u8` + DASH `.mpd`.
//!
//! The manifest is the index a player reads *before* any media: it names the init
//! segment, lists every media segment and its exact duration, and (at the master /
//! MPD level) advertises the bitrate ladder so the player can choose and switch.
//! HLS is a line-oriented text format (`#EXT-X-…` tags); DASH is XML with a
//! `SegmentTemplate`/`SegmentTimeline` model. Both describe the *same* segments V2
//! produced — this module is the mapping.
//!
//! These are pure functions (segment list → string): no I/O, trivially testable
//! against golden files.

use crate::isobmff::Track;
use crate::segment::SegmentIndex;

/// One rung of the ABR ladder, for the HLS master playlist / DASH adaptation set.
#[derive(Debug, Clone)]
pub struct RenditionInfo {
    /// Rendition id (e.g. `720p`) — also the path segment in its media-playlist URI.
    pub id: String,
    /// Peak bitrate in bits/sec — the HLS `BANDWIDTH` attribute. A player picks a
    /// rung from this vs. its measured throughput.
    pub bandwidth: u32,
    /// Coded resolution for the `RESOLUTION` attribute (0×0 ⇒ omit).
    pub width: u16,
    pub height: u16,
    /// URI of this rendition's media playlist, relative to the master.
    pub uri: String,
}

/// Render the **HLS media playlist** for one rendition (V3 core).
///
/// TODO(V3): emit, in order:
///   `#EXTM3U`
///   `#EXT-X-VERSION:7`                     (7+ is required for fMP4 / `EXT-X-MAP`)
///   `#EXT-X-TARGETDURATION:<ceil(max seg seconds)>`
///   `#EXT-X-MEDIA-SEQUENCE:0`
///   `#EXT-X-PLAYLIST-TYPE:VOD`
///   `#EXT-X-MAP:URI="init.mp4"`           (the init segment)
///   then per segment:
///     `#EXTINF:<seconds with enough precision>,`
///     `seg/<index>`                        (matches the delivery route)
///   `#EXT-X-ENDLIST`                       (VOD: the list is complete)
/// The summed `#EXTINF`s must equal the track duration within a frame — carry the
/// real per-segment durations from `index`, don't re-round the target.
pub fn hls_media_playlist(index: &SegmentIndex, track: &Track, _target_secs: f64) -> String {
    let _ = (index, track);
    todo!("V3: render the HLS media playlist (EXT-X-MAP, EXTINF per segment, ENDLIST)")
}

/// Render the **HLS master playlist** advertising the rendition ladder (V3/V4).
///
/// TODO(V3): emit `#EXTM3U` + `#EXT-X-VERSION:7`, then one
/// `#EXT-X-STREAM-INF:BANDWIDTH=<bps>,RESOLUTION=<w>x<h>` line per rendition
/// followed by its media-playlist `uri`. This is what makes ABR possible: the
/// player reads the ladder here and switches rungs as bandwidth changes.
pub fn hls_master_playlist(renditions: &[RenditionInfo]) -> String {
    let _ = renditions;
    todo!("V3: render the HLS master playlist (one EXT-X-STREAM-INF per rendition)")
}

/// Render the **DASH MPD** for one rendition's segments (V3).
///
/// TODO(V3): emit a `static` (VOD) MPD: `MPD` → `Period` → `AdaptationSet` →
/// `Representation` with the codec/bandwidth, and a `SegmentList` (or
/// `SegmentTemplate` + `SegmentTimeline`) referencing `init.mp4` and each
/// `seg/<index>` with its duration in the MPD timescale. Same segments as the HLS
/// playlist, described the DASH way — proving you understand both models map onto
/// the one segment list.
pub fn dash_mpd(index: &SegmentIndex, track: &Track) -> String {
    let _ = (index, track);
    todo!("V3: render the DASH MPD (Representation + SegmentList over the same segments)")
}

#[cfg(test)]
mod tests {
    // TODO(V3): golden-file tests against the committed fixture:
    //   - `hls_media_playlist` matches expected output byte-for-byte, and its
    //     summed EXTINF == track duration within one frame (`renders_hls_media_playlist`);
    //   - `hls_master_playlist` lists every rendition with BANDWIDTH + RESOLUTION;
    //   - `dash_mpd` validates against a DASH conformance checker (`renders_dash_mpd`).
}
