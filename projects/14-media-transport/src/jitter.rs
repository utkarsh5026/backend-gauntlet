//! V2 — the jitter buffer: a smooth playout out of a jittery arrival.
//!
//! The network hands you packets early, late, out of order, and duplicated. Playing each
//! the instant it lands would stutter. The jitter buffer holds a small window (a *target
//! delay*), releases packets **in sequence order** once they've waited it out, drops
//! duplicates and packets too late to use, and tracks the gaps so V3 can NACK them — all
//! at the cost of a small, bounded amount of added latency (the smoothness ↔ latency
//! tradeoff this buffer *is*).
//!
//! Two subtleties: the 16-bit sequence number **wraps** (65535 → 0), so it's unwrapped to
//! a monotonic index for ordering; and the buffer is **capped** (a broken peer that never
//! marks a frame, or floods future sequence numbers, must not grow memory without bound).

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use crate::error::Result;
use crate::rtp::RtpPacket;

/// A snapshot of the buffer's quality signals, surfaced to metrics and RTCP reports.
#[derive(Debug, Clone, Copy, Default)]
pub struct JitterStats {
    /// Smoothed interarrival jitter estimate, in clock-rate ticks (RFC 3550).
    pub jitter: f64,
    /// Packets currently held.
    pub buffered_packets: usize,
    /// Duplicates discarded so far.
    pub duplicates: u64,
    /// Packets dropped for arriving after their playout deadline.
    pub late: u64,
    /// Sequence gaps skipped because the packet never arrived.
    pub skipped: u64,
}

/// A reorder + playout buffer for one stream (SSRC).
///
/// Keyed by the **unwrapped** sequence number so ordering is correct across the 16-bit
/// wrap; `capacity` bounds it against a hostile flood.
pub struct JitterBuffer {
    target_delay: Duration,
    clock_rate: u32,
    capacity: usize,
    /// Held packets, keyed by unwrapped sequence, each with its arrival instant (for the
    /// playout deadline).
    packets: BTreeMap<u64, (RtpPacket, Instant)>,
    /// Anchor for unwrapping the 16-bit sequence to a monotonic index. `None` until the
    /// first packet establishes the base.
    base_sequence: Option<u16>,
    /// Highest unwrapped sequence admitted so far (gaps below it are NACK candidates).
    highest: u64,
    stats: JitterStats,
}

impl JitterBuffer {
    pub fn new(target_delay: Duration, clock_rate: u32, capacity: usize) -> Self {
        Self {
            target_delay,
            clock_rate,
            capacity,
            packets: BTreeMap::new(),
            base_sequence: None,
            highest: 0,
            stats: JitterStats::default(),
        }
    }

    /// True when nothing is buffered — the session's playout tick uses this to stay idle
    /// (and panic-free) until real traffic arrives.
    pub fn is_empty(&self) -> bool {
        self.packets.is_empty()
    }

    /// The latest quality snapshot.
    pub fn stats(&self) -> JitterStats {
        JitterStats {
            buffered_packets: self.packets.len(),
            ..self.stats
        }
    }

    /// Admit a packet that arrived at `arrival` (V2).
    ///
    /// TODO(V2): unwrap the packet's 16-bit sequence to a monotonic index (establish
    /// `base_sequence` on the first packet; detect wrap by comparing against `highest`);
    /// **drop and count a duplicate** (index already present); **drop and count a late**
    /// packet (below the playout floor / already released); otherwise insert into
    /// `packets`, advance `highest`, and update the RFC 3550 interarrival **jitter**
    /// estimate from the arrival spacing vs. the RTP-timestamp spacing. Enforce `capacity`
    /// — never let `packets` grow past it.
    pub fn insert(&mut self, packet: RtpPacket, arrival: Instant) -> Result<()> {
        let _ = (
            packet,
            arrival,
            self.clock_rate,
            self.capacity,
            &mut self.base_sequence,
            &mut self.highest,
            &mut self.packets,
            &mut self.stats,
        );
        todo!("V2: unwrap seq, drop dup/late, insert in order, update jitter estimate")
    }

    /// Release the next complete frame ready for playout, if any (V2).
    ///
    /// TODO(V2): if the oldest buffered packet has waited out `target_delay` (by `now -
    /// arrival`), release the next **complete frame** — the run of consecutive packets in
    /// sequence order up to and including one with the **marker** bit — as a `Vec`. Return
    /// `None` while the head frame is still incomplete *and* within its delay window; once
    /// past the window with a gap that never filled, **skip** it (count `skipped`) so the
    /// buffer never stalls forever on a packet that isn't coming.
    pub fn pop_frame(&mut self, now: Instant) -> Option<Vec<RtpPacket>> {
        let _ = (now, self.target_delay, &mut self.packets, &mut self.stats);
        todo!("V2: release the next in-order complete frame once it has aged past target_delay")
    }

    /// The missing sequence numbers below the highest received — the NACK candidates V3
    /// turns into feedback (V2).
    ///
    /// TODO(V2): scan for gaps between the lowest still-relevant sequence and `highest` and
    /// return the missing 16-bit sequence numbers (re-wrapped from the unwrapped indices).
    pub fn missing(&self) -> Vec<u16> {
        let _ = (&self.packets, self.base_sequence, self.highest);
        todo!("V2: report sequence gaps below `highest` as NACK candidates")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove the buffer:
    //   - `reorders_out_of_order`: packets inserted 3,1,2 play out 1,2,3;
    //   - `drops_duplicates`: a repeated sequence is counted once, played once;
    //   - `orders_across_sequence_wrap`: …65534,65535,0,1 order correctly across the wrap;
    //   - `gap_is_skipped_not_stalled`: a never-arriving packet is skipped after the delay
    //     window, not waited on forever;
    //   - `playout_is_always_ordered`: a property test that any insert order plays sorted.
}
