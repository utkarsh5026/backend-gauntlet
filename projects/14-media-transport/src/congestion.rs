//! V4 — congestion control: pace to the bandwidth the path actually has.
//!
//! UDP won't slow you down when the link is full — it just drops your packets, spikes the
//! queueing delay, and quietly destroys the stream. So the sender owns congestion control:
//! **estimate** the available bandwidth from feedback and **pace** output to match, backing
//! off when the path congests and probing up when it clears.
//!
//! Two signals drive the estimate. **Loss-based**: rising loss ⇒ overshoot ⇒ cut the rate
//! (AIMD-style); near-zero loss ⇒ probe higher. **Delay-based**: a growing one-way delay
//! gradient means a queue is building *before* it overflows into loss — the earlier, gentler
//! signal a real controller (Google's GCC) blends in. You choose the sophistication — even a
//! clean loss-based AIMD with a pacer passes — but it must **converge, back off, and
//! recover**, and the target is always **clamped** to `[min, max]`.
//!
//! The controller also owns the **pacer**: a leaky-bucket/token gate that spreads packets
//! across the frame interval at the target rate instead of bursting a whole frame at once —
//! bursting is what builds the queue (and the latency) you're trying to avoid.

use std::time::Instant;

/// Loss- (and optionally delay-) based bitrate estimator with a token pacer.
pub struct CongestionController {
    target_bitrate: u32,
    min_bitrate: u32,
    max_bitrate: u32,
    /// Token-bucket level for pacing, in bytes; refilled at `target_bitrate`.
    tokens: f64,
    /// When the pacer last refilled its tokens.
    last_refill: Instant,
}

impl CongestionController {
    pub fn new(start_bitrate: u32, min_bitrate: u32, max_bitrate: u32) -> Self {
        Self {
            target_bitrate: start_bitrate.clamp(min_bitrate, max_bitrate),
            min_bitrate,
            max_bitrate,
            tokens: 0.0,
            last_refill: Instant::now(),
        }
    }

    /// The current target send rate, in bits/sec (for the pacer, the metrics gauge, and the
    /// media source to size frames against).
    pub fn target_bitrate(&self) -> u32 {
        self.target_bitrate
    }

    /// Update the estimate from a receiver report's loss + jitter (V4).
    ///
    /// TODO(V4): apply the control law. Sustained loss (`fraction_lost` above a threshold)
    /// **multiplicatively decreases** the target; a clean path **additively increases** it
    /// toward `max_bitrate`. Optionally fold in a delay signal. Always **clamp** the result
    /// to `[min_bitrate, max_bitrate]` — a hostile/garbage feedback value must not drive it
    /// out of range, negative, or zero-stuck.
    pub fn on_receiver_report(&mut self, fraction_lost: u8, jitter: u32) {
        let _ = (
            fraction_lost,
            jitter,
            &mut self.target_bitrate,
            self.min_bitrate,
            self.max_bitrate,
        );
        todo!("V4: AIMD/GCC-lite update of target_bitrate, clamped to [min,max]")
    }

    /// Feed one packet's transit-delay sample (send vs. arrival spacing) for the delay-based
    /// signal (V4, optional but where GCC gets its early warning).
    ///
    /// TODO(V4): accumulate the one-way delay **gradient**; a persistently rising gradient
    /// is a building queue — treat it like (gentler) loss and trim the target before the
    /// queue overflows. A flat/negative gradient is headroom to probe. If you implement a
    /// pure loss-based controller, this can stay a no-op — say so in `docs/14-design.md`.
    pub fn on_delay_sample(&mut self, arrival_delta_ms: f64, send_delta_ms: f64) {
        let _ = (arrival_delta_ms, send_delta_ms, &mut self.target_bitrate);
        todo!("V4: update the delay-gradient estimate (or document a no-op loss-only law)")
    }

    /// Pacer gate: may a packet of `bytes` be sent at `now` under the current target (V4)?
    ///
    /// TODO(V4): refill the token bucket by `target_bitrate * (now - last_refill)` (bytes),
    /// cap it at a small burst budget, and return whether `tokens >= bytes`. This is what
    /// spreads a frame's packets across the frame interval instead of firing them at once.
    pub fn can_send(&mut self, now: Instant, bytes: usize) -> bool {
        let _ = (
            now,
            bytes,
            &mut self.tokens,
            &mut self.last_refill,
            self.target_bitrate,
        );
        todo!("V4: token-bucket pacer — refill by rate*elapsed, gate on tokens >= bytes")
    }

    /// Account for a packet actually sent (debit the pacer) (V4).
    ///
    /// TODO(V4): subtract `bytes` worth of tokens.
    pub fn on_sent(&mut self, bytes: usize) {
        let _ = (bytes, &mut self.tokens);
        todo!("V4: debit the token bucket by the sent bytes")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove the controller:
    //   - `bitrate_backs_off_on_loss`: sustained high `fraction_lost` lowers the target;
    //   - `bitrate_recovers_on_clear_path`: zero loss climbs it back toward max;
    //   - `bitrate_stays_clamped`: no feedback drives it out of [min, max];
    //   - `pacer_spreads_sends`: under a fixed target, `can_send` gates a burst so sends are
    //     spaced ~evenly across the interval, not all at once.
}
