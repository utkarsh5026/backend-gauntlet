//! V4 — Low-Latency HLS playlist + blocking delivery: break the latency wall.
//!
//! A regular live playlist is a rolling window of `#EXTINF` segments the player
//! re-fetches every target-duration; its latency floor is ~3 segments. LL-HLS gets
//! under it by publishing, for the still-forming segment, `#EXT-X-PART` lines (one per
//! ~200 ms part), an `#EXT-X-PRELOAD-HINT` for the next part that doesn't exist yet,
//! and `#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES`. The player then requests the
//! playlist with `_HLS_msn`/`_HLS_part`, and the server **holds** that request until
//! the named part exists — returning the instant it does.
//!
//! The park-until-signalled mechanism lives in `LiveStream::await_edge` (wired); this
//! module owns the two things that are the V4 learning: **rendering** the LL-HLS
//! playlist from the window, and **mapping** a blocking-reload request onto the edge to
//! wait for (with a bounded timeout).

use std::time::Duration;

use crate::error::AppError;
use crate::live::{LiveEdge, LiveStream};

/// The LL-HLS blocking-reload query parameters (`index.m3u8?_HLS_msn=..&_HLS_part=..`).
#[derive(Debug, Clone, Copy, Default)]
pub struct ReloadParams {
    /// `_HLS_msn`: the media sequence number the player wants the playlist to include.
    pub msn: Option<u64>,
    /// `_HLS_part`: the part index within that media sequence.
    pub part: Option<u64>,
    /// `_HLS_skip=YES`: the player accepts a delta playlist (skipping old segments).
    pub skip: bool,
}

/// The longest a blocking reload will be held before returning what we have anyway, so
/// a bogus `_HLS_msn` far in the future can't pin a request open forever.
const MAX_BLOCK: Duration = Duration::from_secs(5);

/// Serve the LL-HLS media playlist for a stream, honoring blocking-reload params (V4).
///
/// The wait mechanism is wired; the rendering ([`render_media_playlist`]) and the
/// exact "which edge satisfies this request" mapping are the V4 `todo!()`s.
pub async fn media_playlist(stream: &LiveStream, params: ReloadParams) -> Result<String, AppError> {
    // Blocking reload: if the player named an (msn, part) it doesn't have yet, park
    // until the live edge reaches it (or the bound elapses) — never busy-poll a 404.
    if let Some(msn) = params.msn {
        let target = LiveEdge {
            msn,
            part: params.part.unwrap_or(0),
        };
        if stream.edge() < target {
            let _ = tokio::time::timeout(MAX_BLOCK, stream.await_edge(target)).await;
        }
    }
    render_media_playlist(stream)
}

/// Render the LL-HLS media playlist text from the live window (V4 core).
///
/// TODO(V4): emit, in order:
///   `#EXTM3U`
///   `#EXT-X-VERSION:9`                       (parts need v9+)
///   `#EXT-X-TARGETDURATION:<ceil(max seg secs)>`
///   `#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=<~3×part>,CAN-SKIP-UNTIL=…`
///   `#EXT-X-PART-INF:PART-TARGET=<part secs>`
///   `#EXT-X-MEDIA-SEQUENCE:<oldest segment's msn>`
///   `#EXT-X-MAP:URI="init.mp4"`
///   then per segment in the window:
///     `#EXT-X-PROGRAM-DATE-TIME:<wall clock>`  (once, on the first)
///     one `#EXT-X-PART:DURATION=<d>,URI="part/<msn>/<i>.m4s"[,INDEPENDENT=YES]` per part
///     `#EXTINF:<seg secs>,` + `seg/<msn>.m4s`   (only for a *complete* segment)
///   and for the segment still forming at the live edge, a trailing
///     `#EXT-X-PRELOAD-HINT:TYPE=PART,URI="part/<msn>/<next-i>.m4s"`
///   plus `#EXT-X-ENDLIST` iff the stream has ended.
/// Use `stream.snapshot()` for the window. The `#EXT-X-MEDIA-SEQUENCE` and per-part
/// indices must be **monotonic** across reloads — that's what a player relies on.
fn render_media_playlist(stream: &LiveStream) -> Result<String, AppError> {
    // `snapshot()` is the window + ended flag a real render walks; threaded so the
    // entry point is obvious.
    let _ = stream.snapshot();
    todo!("V4: render the LL-HLS media playlist (parts, preload hint, server-control)")
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove the playlist + blocking reload:
    //   - the rendered playlist carries the required LL-HLS tags and a correct rolling
    //     `#EXT-X-MEDIA-SEQUENCE` (`playlist_has_llhls_tags`);
    //   - consecutive reloads see the media sequence / part indices advance monotonically
    //     with no gap or repeat (`media_sequence_advances`);
    //   - a blocking `_HLS_msn/_HLS_part` request for a not-yet-produced part is held and
    //     returns exactly when that part is pushed — never stale, never a 404
    //     (`blocking_reload_unblocks_on_part`);
    //   - a request past the window / beyond the bound returns promptly, not a hang.
}
