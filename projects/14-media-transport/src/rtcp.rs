//! V3 — RTCP + selective retransmission (NACK): recover the losses that still matter.
//!
//! RTCP is the back-channel that rides alongside RTP. A **Receiver Report** (RR) tells the
//! sender what the receiver sees — fraction lost, cumulative lost, highest sequence,
//! interarrival jitter. A **generic NACK** (RTPFB, feedback message type 1) names lost
//! packets compactly: a **PID** (a base sequence) plus a 16-bit **BLP** bitmask covering
//! the next 16 sequence numbers, so one FCI word requests up to 17 packets. The sender
//! keeps a bounded **retransmit cache** of recently sent packets and, on a NACK, resends
//! the ones it still holds — but only the ones that can still arrive **before their
//! playout deadline**. Reliability you *choose*, per packet, against a clock — not TCP's
//! total reliability you pay for on every byte.
//!
//! Everything here parses hostile bytes off an open UDP port, so every length word is
//! range-checked before use.

use std::collections::VecDeque;

use bytes::Bytes;

use crate::error::{Result, TransportError};
use crate::rtp::RtpPacket;

/// RTCP packet type: Sender Report.
pub const PT_SR: u8 = 200;
/// RTCP packet type: Receiver Report.
pub const PT_RR: u8 = 201;
/// RTCP packet type: Source Description.
pub const PT_SDES: u8 = 202;
/// RTCP packet type: goodbye.
pub const PT_BYE: u8 = 203;
/// RTCP packet type: transport-layer feedback (generic NACK lives here).
pub const PT_RTPFB: u8 = 205;
/// RTPFB feedback message type for a generic NACK (RFC 4585 §6.2.1).
pub const FMT_GENERIC_NACK: u8 = 1;

/// A Receiver Report block: one stream's reception quality from the receiver's side.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceiverReport {
    /// SSRC of the receiver sending this report.
    pub reporter_ssrc: u32,
    /// SSRC of the stream being reported on.
    pub media_ssrc: u32,
    /// Fraction of packets lost since the last report (8.8 fixed-point numerator).
    pub fraction_lost: u8,
    /// Cumulative packets lost (24-bit, signed per spec).
    pub cumulative_lost: u32,
    /// Extended highest sequence number received.
    pub highest_sequence: u32,
    /// Interarrival jitter, in clock-rate ticks.
    pub jitter: u32,
}

/// A generic NACK: "resend these sequence numbers". The wire form packs them as PID+BLP
/// FCI words; this is the decoded set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Nack {
    /// SSRC of the receiver asking.
    pub sender_ssrc: u32,
    /// SSRC of the stream whose packets are missing.
    pub media_ssrc: u32,
    /// The missing sequence numbers requested.
    pub lost: Vec<u16>,
}

impl Nack {
    /// Build a NACK for a set of missing sequence numbers.
    pub fn from_missing(sender_ssrc: u32, media_ssrc: u32, missing: &[u16]) -> Nack {
        Nack {
            sender_ssrc,
            media_ssrc,
            lost: missing.to_vec(),
        }
    }

    /// Encode the missing set into RFC 4585 PID+BLP FCI words (V3).
    ///
    /// TODO(V3): group the sorted, deduped `lost` sequences into FCI words — each word is a
    /// **PID** (a base sequence) plus a 16-bit **BLP** bitmask where bit *i* means "PID+1+i
    /// is also lost". One word covers up to 17 numbers; spill into more words as needed.
    /// Handle the sequence **wrap** (numbers just below and just above 0 in one word).
    pub fn to_fci(&self) -> Vec<u32> {
        let _ = &self.lost;
        todo!("V3: pack missing sequences into PID+BLP FCI words (16-bit bitmask)")
    }
}

/// One parsed RTCP packet from a compound datagram (the subset this transport acts on).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RtcpPacket {
    ReceiverReport(ReceiverReport),
    Nack(Nack),
    /// Source is leaving (RFC 3550 BYE) — the peer's SSRC list.
    Bye(Vec<u32>),
}

impl RtcpPacket {
    /// Parse a compound RTCP datagram (several stacked packets) into its parts (V3).
    ///
    /// TODO(V3): walk the datagram packet-by-packet. Each RTCP packet starts with a common
    /// header: version(2)|padding|count(5), packet type (8), and a **length** word (in
    /// 32-bit words minus one). **Validate that length against the remaining buffer before
    /// advancing** — a length that overruns is `Err(Malformed)`, never an OOB read. Decode
    /// RR (`PT_RR`), generic NACK (`PT_RTPFB` + `FMT_GENERIC_NACK`, unpacking PID+BLP), and
    /// BYE (`PT_BYE`); skip types you don't act on. Ignore/skip SR/SDES bodies you parse
    /// but don't use.
    pub fn parse_compound(buf: &[u8]) -> Result<Vec<RtcpPacket>> {
        if buf.len() < 4 {
            return Err(TransportError::Truncated {
                need: 4,
                got: buf.len(),
            });
        }
        todo!("V3: walk the compound RTCP packet, length-checking each sub-packet")
    }

    /// Serialize this RTCP packet to a datagram (V3).
    ///
    /// TODO(V3): write the common header (version|count, PT, length-in-words) then the
    /// body — RR block, NACK FCI ([`Nack::to_fci`]), or BYE SSRC list. `serialize` then
    /// `parse_compound` must round-trip the fields.
    pub fn serialize(&self) -> Bytes {
        todo!("V3: encode the RTCP common header + body (RR / NACK FCI / BYE)")
    }
}

/// A bounded history of recently sent packets, so a NACK can be answered by resending the
/// exact bytes — as long as they're still in the window and not past their deadline.
///
/// A ring bounded by `capacity`: the oldest packet is evicted once full, which is also the
/// natural "too old to be useful" boundary.
pub struct RetransmitCache {
    capacity: usize,
    packets: VecDeque<RtpPacket>,
}

impl RetransmitCache {
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity,
            packets: VecDeque::with_capacity(capacity),
        }
    }

    /// Record a just-sent packet, evicting the oldest if at capacity (V3).
    ///
    /// TODO(V3): push `packet` and drop from the front until `packets.len() <= capacity`.
    /// This eviction *is* the staleness bound — a packet gone from the cache is one you've
    /// decided is too old to usefully retransmit.
    pub fn record(&mut self, packet: RtpPacket) {
        let _ = (packet, self.capacity, &mut self.packets);
        todo!("V3: push into the ring, evicting the oldest past capacity")
    }

    /// Fetch a still-cached packet by its 16-bit sequence number, if present (V3).
    ///
    /// TODO(V3): find the packet whose header sequence matches `sequence` (mind the wrap);
    /// return `None` if it has been evicted — a miss is normal, never an error.
    pub fn get(&self, sequence: u16) -> Option<RtpPacket> {
        let _ = (sequence, &self.packets);
        todo!("V3: look up a cached packet by sequence number")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V3): prove the control plane:
    //   - `rtcp_roundtrips`: an RR and a NACK serialize∘parse to the same fields, and a
    //     compound packet parses into its parts;
    //   - `nack_bitmask_packs_missing`: a set of missing seqs → FCI words → the same set;
    //   - `nack_packs_across_wrap`: the bitmask spans the 65535→0 boundary correctly;
    //   - `truncated_rtcp_errors`: a short/garbage datagram is `Err`, never a panic;
    //   - `retransmit_cache_evicts`: past capacity the oldest packet is gone (get→None).
}
