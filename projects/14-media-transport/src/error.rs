//! The transport-plane error type.
//!
//! These are the errors of parsing/building the RTP and RTCP wire formats and running the
//! jitter buffer — the media plane is UDP, so a bad datagram is *dropped*, not turned into
//! an HTTP status. The admin HTTP surface (`admin.rs`) is trivially infallible
//! (`/healthz`, `/metrics`) and doesn't use this type.
//!
//! Every variant here is a *bounded, non-fatal* failure: an attacker (or a broken peer)
//! can put arbitrary bytes on an open UDP port, so a length that doesn't add up, a version
//! that isn't 2, or an MTU too small to hold a header must all be a recoverable `Err` that
//! ends up dropping one datagram — never a panic, an out-of-bounds index, or an allocation
//! sized by the wire.

/// A convenience alias so the vertical modules can write `Result<RtpPacket>`.
pub type Result<T, E = TransportError> = std::result::Result<T, E>;

#[derive(Debug, thiserror::Error)]
pub enum TransportError {
    /// A datagram (or a field within it) was shorter than the layout requires.
    #[error("truncated: need at least {need} bytes, got {got}")]
    Truncated { need: usize, got: usize },

    /// An RTP packet whose version bits were not `2`.
    #[error("unsupported RTP version {0} (expected 2)")]
    BadVersion(u8),

    /// A well-sized but internally inconsistent packet (bad FU header, RTCP length word
    /// that overruns the buffer, NACK FCI count that doesn't fit, …).
    #[error("malformed packet: {0}")]
    Malformed(String),

    /// The configured MTU can't hold even an RTP header, so packetization is impossible.
    #[error("mtu too small to packetize: {0}")]
    Oversized(String),
}
