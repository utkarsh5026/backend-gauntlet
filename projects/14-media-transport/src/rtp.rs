//! V1 — RTP packetization + depacketization: turn a frame into datagrams and back.
//!
//! RTP is the thin header that makes a *media stream* out of lonely UDP datagrams. The
//! header is 12 bytes (before optional CSRCs):
//!
//! ```text
//!  0                   1                   2                   3
//!  0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! |V=2|P|X|  CC   |M|     PT      |       sequence number         |
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! |                           timestamp                           |
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! |                             SSRC                              |
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! |                        CSRC (0..=15) …                        |
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! ```
//!
//! The **sequence number** increments once per packet (and wraps at 65535); the
//! **timestamp** is the *media* clock (90 kHz for video), so every packet of one frame
//! shares it and it jumps by the sampling interval between frames. The **marker bit** is
//! set on the last packet of a frame. A frame bigger than the path MTU is split with
//! H.264 **FU-A** fragmentation (start/end bits mark the pieces); small NALs ship whole.

use bytes::Bytes;

use crate::error::{Result, TransportError};

/// The only RTP version in use.
pub const RTP_VERSION: u8 = 2;
/// Fixed RTP header size before any CSRC entries.
pub const RTP_MIN_HEADER: usize = 12;
/// Video media clock (Hz) — the timestamp is in these ticks, not milliseconds.
pub const H264_CLOCK_RATE: u32 = 90_000;

/// A parsed RTP header (the fixed 12 bytes plus any CSRC list).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RtpHeader {
    /// Last-packet-of-frame marker.
    pub marker: bool,
    /// 7-bit payload type (e.g. 96 for a dynamic H.264 mapping).
    pub payload_type: u8,
    /// Per-packet sequence number (wraps at 65535).
    pub sequence: u16,
    /// Media timestamp in clock-rate ticks (shared across a frame's packets).
    pub timestamp: u32,
    /// Synchronization source id — identifies the stream.
    pub ssrc: u32,
    /// Contributing source ids (empty for a single, unmixed source).
    pub csrc: Vec<u32>,
}

impl RtpHeader {
    /// Parse the header off the front of `buf`, returning it and the header length (so the
    /// caller can slice the payload).
    ///
    /// TODO(V1): read the first 12 bytes big-endian — version (top 2 bits, reject != 2),
    /// the CC count (low 4 bits of byte 0) to know how many trailing CSRC words to read,
    /// the marker + payload type (byte 1), sequence (u16), timestamp (u32), SSRC (u32),
    /// then `cc` CSRC words. **Range-check every read** against `buf.len()` before
    /// indexing — a truncated header must be `Err(Truncated)`, never a panic.
    pub fn parse(buf: &[u8]) -> Result<(RtpHeader, usize)> {
        if buf.len() < RTP_MIN_HEADER {
            return Err(TransportError::Truncated {
                need: RTP_MIN_HEADER,
                got: buf.len(),
            });
        }
        let version = buf[0] >> 6;
        if version != RTP_VERSION {
            return Err(TransportError::BadVersion(version));
        }
        todo!("V1: decode marker/PT/seq/ts/SSRC + the CSRC list, returning (header, header_len)")
    }

    /// Serialize this header into `out`.
    ///
    /// TODO(V1): the inverse of [`parse`](Self::parse) — write version|padding|extension|CC
    /// (CC = `csrc.len()`), marker|PT, then sequence/timestamp/SSRC and each CSRC, all
    /// big-endian. `write` then `parse` must reproduce this header exactly.
    pub fn write(&self, out: &mut bytes::BytesMut) {
        let _ = (out, self.marker, self.payload_type, self.sequence);
        let _ = (self.timestamp, self.ssrc, &self.csrc);
        todo!("V1: encode the RTP header (fixed 12 bytes + CSRC list) big-endian")
    }
}

/// One RTP packet: a header plus its media payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RtpPacket {
    pub header: RtpHeader,
    pub payload: Bytes,
}

impl RtpPacket {
    /// Parse a whole datagram into header + payload.
    ///
    /// TODO(V1): [`RtpHeader::parse`] then slice off the payload after the header length.
    pub fn parse(buf: &[u8]) -> Result<RtpPacket> {
        let _ = buf;
        todo!("V1: parse header, then take the remainder as the payload")
    }

    /// Serialize the whole packet (header + payload) into a fresh buffer.
    ///
    /// TODO(V1): write the header via [`RtpHeader::write`], then append the payload.
    pub fn serialize(&self) -> Bytes {
        todo!("V1: write header + payload into a Bytes")
    }
}

/// Splits access units into RTP packets, stamping each with the stream's SSRC, payload
/// type, and a monotonically increasing sequence number.
///
/// Holds the running sequence number (the one piece of per-stream state packetization
/// needs) plus the immutable SSRC / payload type / MTU budget.
pub struct Packetizer {
    ssrc: u32,
    payload_type: u8,
    mtu: usize,
    sequence: u16,
}

impl Packetizer {
    pub fn new(ssrc: u32, payload_type: u8, mtu: usize, initial_sequence: u16) -> Self {
        Self {
            ssrc,
            payload_type,
            mtu,
            sequence: initial_sequence,
        }
    }

    /// The next sequence number this packetizer will assign (for the retransmit cache /
    /// tests to reason about ordering).
    pub fn next_sequence(&self) -> u16 {
        self.sequence
    }

    /// Packetize one access unit (already-encoded bytes) at `rtp_timestamp` into RTP
    /// packets (V1).
    ///
    /// TODO(V1): walk the access unit's NAL units; a NAL that fits in
    /// `mtu - RTP_MIN_HEADER` ships as a **single** packet, a larger one is split into
    /// **FU-A** fragments (each with a fragmentation-unit header carrying start/end bits).
    /// Assign each packet the next `sequence` (wrapping), the shared `rtp_timestamp`, the
    /// stream `ssrc`/`payload_type`, and set the **marker** bit on the **last** packet of
    /// the frame only. No emitted packet may exceed `mtu`.
    pub fn packetize(&mut self, access_unit: &[u8], rtp_timestamp: u32) -> Result<Vec<RtpPacket>> {
        if self.mtu <= RTP_MIN_HEADER {
            return Err(TransportError::Oversized(format!(
                "mtu {} cannot hold a {RTP_MIN_HEADER}-byte RTP header",
                self.mtu
            )));
        }
        let _ = (
            access_unit,
            rtp_timestamp,
            self.ssrc,
            self.payload_type,
            &mut self.sequence,
        );
        todo!("V1: fragment (FU-A) / single-NAL packetize the access unit into RtpPackets")
    }
}

/// Reassemble one frame's RTP packets (in sequence order) back into an access unit (V1).
///
/// TODO(V1): concatenate the packets' payloads, undoing FU-A fragmentation — a single-NAL
/// payload is emitted as-is; a run of FU-A fragments (from the start-bit packet to the
/// end-bit packet) is stitched back into the original NAL. A missing fragment (a gap in
/// the run, or no end bit) must be reported as `Err`, not emitted as corrupt bytes.
pub fn depacketize(packets: &[RtpPacket]) -> Result<Bytes> {
    let _ = packets;
    todo!("V1: reassemble a frame's payloads (single-NAL + FU-A) into one access unit")
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the codec:
    //   - `rtp_header_roundtrips`: parse∘write and write∘parse are identity (incl. CSRCs);
    //   - a header shorter than 12 bytes is `Err`, never a panic (`short_header_errors`);
    //   - `fragmented_frame_reassembles`: a >MTU access unit packetizes into several
    //     packets that `depacketize` reassembles to the exact original bytes;
    //   - `packet_sequence_and_marker_are_correct`: within a frame, sequence numbers are
    //     consecutive, timestamps equal, and only the last packet has the marker bit.
}
