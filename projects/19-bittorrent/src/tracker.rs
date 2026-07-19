//! V3 — Tracker announce: peer discovery over HTTP *and* UDP.
//!
//! A tracker answers one question: "who else has infohash X?" You announce (with your
//! progress) and it returns a list of peers plus an `interval` telling you how long to
//! wait before asking again. Two transports, and you implement both:
//!
//!   - **HTTP** (BEP 3): a `GET /announce?...` whose reply is a bencoded dict. The
//!     `peers` value is usually the **compact** form — 6 bytes per peer (4-byte IPv4 +
//!     2-byte big-endian port) — because a list of dicts is wasteful at swarm scale.
//!   - **UDP** (BEP 15): a tiny binary protocol. You first `connect` (get a
//!     connection-id that expires in ~1 min — cheap anti-spoofing so a forged source IP
//!     can't announce), then `announce`. Everything is big-endian; every request/reply
//!     is paired by a random transaction-id.
//!
//! An announce is a periodic *side effect*, not a blocking request/response on the
//! download path — you `started` on join, re-announce on the interval, and `stopped` on
//! a clean exit. One dead tracker must not sink the download.

use std::net::SocketAddr;

use crate::error::AppError;
use crate::types::{InfoHash, PeerId};

/// The `event` a client reports. `None` is a periodic keep-alive re-announce.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    None,
    Started,
    Stopped,
    Completed,
}

/// What we tell the tracker about ourselves and our progress.
#[derive(Debug, Clone)]
pub struct AnnounceRequest {
    pub info_hash: InfoHash,
    pub peer_id: PeerId,
    /// The port *we* listen on for inbound peers (so others can dial us back).
    pub port: u16,
    pub uploaded: u64,
    pub downloaded: u64,
    /// Bytes still needed — `0` means we're a seed.
    pub left: u64,
    pub event: Event,
}

/// What the tracker tells us back.
#[derive(Debug, Clone)]
pub struct AnnounceResponse {
    /// Seconds to wait before re-announcing — honor it, don't hammer.
    pub interval: u32,
    pub peers: Vec<SocketAddr>,
}

/// Announce over HTTP and parse the bencoded reply.
///
/// TODO(V3): build the query string. The catch: `info_hash` and `peer_id` are **raw
/// 20-byte binary** and must be percent-encoded byte-for-byte (each non-unreserved byte
/// as `%XX`) — you cannot hand them to a normal form-encoder as UTF-8. Add
/// `port/uploaded/downloaded/left/compact=1/event`, GET it, bdecode the response
/// (V1), surface a `failure reason`, and parse `peers` via [`parse_compact_peers`].
pub async fn announce_http(
    http: &reqwest::Client,
    announce_url: &str,
    req: &AnnounceRequest,
) -> Result<AnnounceResponse, AppError> {
    let _ = (http, announce_url, req);
    todo!("V3: HTTP announce — percent-encode the raw infohash/peer_id, GET, bdecode reply")
}

/// Announce over the UDP tracker protocol (BEP 15).
///
/// TODO(V3): the two-step exchange. (1) `connect`: send magic `0x41727101980`, action
/// `0`, a random transaction-id; read back a connection-id. (2) `announce`: send the
/// connection-id, action `1`, transaction-id, infohash, peer-id, downloaded/left/
/// uploaded, event, port; read back `interval` + a compact peer list. All big-endian;
/// re-`connect` if the connection-id has expired; time out and retry a bounded number.
pub async fn announce_udp(
    tracker: SocketAddr,
    req: &AnnounceRequest,
) -> Result<AnnounceResponse, AppError> {
    let _ = (tracker, req);
    todo!("V3: UDP announce — connect handshake then announce, all big-endian (BEP 15)")
}

/// Decode a compact peer list: every 6 bytes is `[a, b, c, d, port_hi, port_lo]` =
/// `a.b.c.d:port` with the port big-endian.
///
/// TODO(V3): chunk `bytes` into 6s (error if not a multiple of 6) and build the addrs.
/// (The IPv6 compact form is 18 bytes/peer — a nice stretch.)
pub fn parse_compact_peers(bytes: &[u8]) -> Result<Vec<SocketAddr>, AppError> {
    let _ = bytes;
    todo!("V3: 6 bytes/peer → SocketAddrV4 (4-byte IPv4 + 2-byte big-endian port)")
}

#[cfg(test)]
mod tests {
    // TODO(V3): prove announce + parsing.
    //   - `parse_compact_peers` decodes a known 6-byte blob to the right addr, and
    //     rejects a non-multiple-of-6 length;
    //   - unit-test the UDP connect/announce frame encode + response decode against
    //     hand-built byte vectors (no network);
    //   - an integration test (needs `docker compose up`): announce to the compose
    //     tracker for a test infohash and get the reference peer back in the list.
}
