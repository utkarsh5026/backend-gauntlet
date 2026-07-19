//! V2 — Selective RTP forwarding: the heart of an SFU.
//!
//! An SFU (Selective Forwarding Unit) is the middle path between two extremes. A **mesh**
//! makes every publisher upload one copy per viewer (N² uploads — dead at ~4 people). An
//! **MCU** decodes everyone, composites one mixed stream, and re-encodes per viewer
//! (CPU-melting, adds a decode/encode latency hop). An **SFU** does neither: it **forwards
//! the publisher's already-encoded RTP packets, unmodified payload, to each subscriber** —
//! one upload from the publisher, one download per subscriber, no transcode. That's how a
//! 50-person call works.
//!
//! The catch is that "forward unmodified" isn't quite true at the *header*. Each subscriber
//! must see one **continuous** RTP stream: a single stable SSRC, sequence numbers with **no
//! gaps** (a browser's jitter buffer treats a gap as loss and NACKs it), and a monotonic
//! timestamp — even though the SFU is *dropping* packets under that subscriber (a deselected
//! simulcast layer, a packet that lost the pacing race) and *switching* which origin stream
//! feeds them mid-call (V3). So per subscriber the SFU keeps a tiny **[`Rewriter`]** that maps
//! whatever origin currently feeds it onto that subscriber's own continuous line, and
//! remembers enough of the mapping to **translate a NACK back**: when a subscriber asks for
//! *its* sequence 4127, the SFU must know that was the origin's sequence 5981 to resend the
//! right cached packet. Reliability, like project 14, is a thing you route — here across a
//! rewrite. (The routing table origin-SSRC → subscribers is plain bookkeeping the wired
//! [`sfu`](crate::sfu) core keeps; the *learning* is the rewriter this file builds.)

use crate::wire::RtpView;

/// Per-subscriber header rewriter — turns a (possibly switching, possibly gappy) origin
/// stream into one continuous RTP stream for a single subscriber.
///
/// Holds the subscriber's stable outbound SSRC plus the running state that keeps the outbound
/// sequence numbers contiguous across dropped/skipped input packets and origin switches, and
/// a bounded history mapping outbound→origin sequence so NACKs can be translated. One
/// `Rewriter` lives per subscriber (not per origin), which is exactly what lets a simulcast
/// switch stay invisible downstream.
pub struct Rewriter {
    out_ssrc: u32,
    // TODO(V2): the state your continuity scheme needs, e.g. the offset between origin and
    // outbound sequence (bumped when you skip an input packet), the last outbound seq/ts,
    // the current origin ssrc (to detect a switch and rebase), and a **bounded** ring of
    // recent (out_seq -> origin_seq) pairs for NACK translation. Keep it fixed-size — a
    // subscriber that never NACKs must not grow this without bound (an OOM guard).
    _state: (),
}

impl Rewriter {
    pub fn new(out_ssrc: u32) -> Self {
        Self {
            out_ssrc,
            _state: (),
        }
    }

    /// The stable SSRC this subscriber sees regardless of origin switches.
    pub fn out_ssrc(&self) -> u32 {
        self.out_ssrc
    }

    /// Rewrite `view` **in place** for this subscriber: stamp the stable outbound SSRC, the
    /// next contiguous outbound sequence number, and a continuous timestamp. Returns the
    /// outbound sequence assigned (so the caller can index a retransmit cache by it).
    ///
    /// TODO(V2): assign `last_out_seq + 1` (wrapping at 65535) — **not** the origin seq, so
    /// gaps the SFU introduced don't show as loss; rebase the timestamp so it stays monotonic
    /// across an origin switch; write all three via [`RtpView::set_ssrc`]/`set_sequence`/
    /// `set_timestamp`; and record the (out_seq → origin_seq) pair in the bounded history.
    pub fn rewrite(&mut self, view: &mut RtpView) -> u16 {
        let _ = (view, self.out_ssrc, &mut self._state);
        todo!("V2: stamp out_ssrc + next contiguous out_seq + continuous ts; record the mapping")
    }

    /// Note that one origin packet was **not** forwarded to this subscriber (deselected
    /// layer / lost the pacing race), so the outbound sequence line stays gapless.
    ///
    /// TODO(V2): advance whatever bookkeeping keeps the next [`rewrite`](Self::rewrite)
    /// contiguous — an SFU-introduced drop must be invisible to the subscriber's jitter buffer.
    pub fn skip(&mut self) {
        let _ = &mut self._state;
        todo!("V2: account for a dropped origin packet without leaving an outbound gap")
    }

    /// Translate a subscriber's NACK (an outbound sequence number) back to the origin sequence
    /// the SFU forwarded, if it's still in the mapping window.
    ///
    /// TODO(V2): look `out_seq` up in the bounded history; `Some(origin_seq)` if known, `None`
    /// if it aged out (too old to usefully retransmit — same deadline logic as project 14).
    /// Correct handling **across the 16-bit wrap** is part of the criterion.
    pub fn to_origin_seq(&self, out_seq: u16) -> Option<u16> {
        let _ = out_seq;
        todo!("V2: map an outbound seq back to the origin seq via the history ring")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove the rewriter:
    //   - `rewrite_is_contiguous`: rewriting a run of origin packets yields outbound seqs that
    //     increase by exactly 1, with a single stable outbound SSRC — even when some origin
    //     packets are `skip`ped (SFU-introduced drops leave no outbound gap);
    //   - `rewrite_survives_origin_switch`: feeding packets from a *different* origin ssrc
    //     mid-stream keeps the subscriber's outbound seq contiguous and timestamp monotonic;
    //   - `nack_translates_back`: an outbound seq maps back to the exact origin seq it came
    //     from, `across_the_wrap` too, and a too-old seq maps to `None`;
    //   - `two_rewriters_are_independent`: two subscribers fed the same origin get independent
    //     seq lines — a `skip` on one doesn't perturb the other.
}
