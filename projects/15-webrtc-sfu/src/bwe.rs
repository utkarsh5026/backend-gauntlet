//! V4 — Bandwidth estimation: figure out how much each subscriber's link can take.
//!
//! Layer selection (V3) is only as good as the number it's handed: *how many bits/sec can
//! this subscriber actually receive right now?* Nobody tells you — you have to **estimate it
//! from feedback**, and the estimate has to move as the link does (someone starts a download,
//! the train enters a tunnel). This is the receive-side congestion controller WebRTC calls
//! GCC, and it runs **per subscriber** at the SFU.
//!
//! Two signals feed it. The **delay-based** signal is the subtle, early one: the SFU (or the
//! subscriber, via **transport-wide congestion control feedback**, TWCC) watches the
//! *inter-arrival delay gradient* — if packets sent 10 ms apart start arriving 15 ms apart, a
//! queue is building on the path **before** it overflows into loss, so ease off now. The
//! **loss-based** signal is the blunt backstop: sustained loss (from RTCP receiver reports)
//! means you're already over, cut hard; near-zero loss means probe up. A real GCC blends
//! both, clamps the result to `[min, max]`, and the SFU's **allocator** then divides that
//! per-subscriber budget across the streams that subscriber is receiving — which is exactly
//! the number [`set_budget`](crate::simulcast::LayerSelector::set_budget) consumes. You
//! choose how sophisticated to go — even a clean loss-based AIMD with a delay-gradient
//! trigger passes — but it must **converge, back off, and recover**.

/// One transport-feedback sample: a packet the SFU sent at `sent` (ms, sender clock) that the
/// subscriber reported receiving at `arrived` (ms, receiver clock). The *gradient* of
/// (arrived − sent) across samples is the delay signal; absolute clock offset cancels out.
#[derive(Debug, Clone, Copy)]
pub struct ArrivalSample {
    pub sent_ms: i64,
    pub arrived_ms: i64,
    pub size_bytes: u32,
}

/// Per-subscriber downlink bandwidth estimator (a GCC-lite receive-side controller).
///
/// Holds the current estimate and the smoothed state its control law needs (a delay-gradient
/// trendline and/or a loss-based AIMD rate), clamped to `[min, max]`.
pub struct BandwidthEstimator {
    estimate_bps: u32,
    min_bps: u32,
    max_bps: u32,
    // TODO(V4): the controller state, e.g. the previous inter-departure/inter-arrival pair
    // for the delay gradient, a smoothed trendline estimate, and the AIMD multiplier the loss
    // signal drives. Keep it O(1) per sample — this runs on the feedback hot path per subscriber.
    _state: (),
}

impl BandwidthEstimator {
    pub fn new(start_bps: u32, min_bps: u32, max_bps: u32) -> Self {
        Self {
            estimate_bps: start_bps.clamp(min_bps, max_bps),
            min_bps,
            max_bps,
            _state: (),
        }
    }

    /// The current estimate (bits/sec), always within `[min, max]`.
    pub fn estimate(&self) -> u32 {
        self.estimate_bps
    }

    /// Update from a batch of transport-feedback samples (the delay-based signal).
    ///
    /// TODO(V4): compute the inter-arrival delay **gradient** across the batch (how arrival
    /// spacing compares to send spacing). A rising gradient (a growing queue) means **over-use**
    /// → decrease the estimate multiplicatively; a flat/negative gradient means the path is
    /// clear → increase it (additively, probing). **Clamp to `[min, max]`** and store. A
    /// garbage/hostile sample batch must not push the estimate out of range or NaN it.
    pub fn on_transport_feedback(&mut self, samples: &[ArrivalSample]) -> u32 {
        let _ = (samples, self.min_bps, self.max_bps, &mut self._state);
        todo!("V4: delay-gradient over-use detector → AIMD the estimate, clamped to [min,max]")
    }

    /// Update from an RTCP receiver report's loss fraction (the loss-based backstop).
    ///
    /// TODO(V4): the WebRTC rule of thumb — `fraction_lost` above ~10% ⇒ multiplicative
    /// decrease (`estimate *= 1 - 0.5*loss`); below ~2% ⇒ additive increase (probe up);
    /// in between ⇒ hold. Clamp to `[min, max]`. The final estimate is the **min** of the
    /// loss-based and delay-based results (the more conservative signal wins).
    pub fn on_loss(&mut self, fraction_lost: f64) -> u32 {
        let _ = (fraction_lost, self.min_bps, self.max_bps, &mut self._state);
        todo!(
            "V4: loss-based AIMD (decrease >10%, increase <2%), clamped, min'd with delay estimate"
        )
    }
}

/// Divides a subscriber's estimated downlink budget across the streams it's receiving.
///
/// With one video stream this is trivial (all of it, minus a safety margin). It becomes real
/// with multiple streams (screen-share + camera): the allocator decides how the budget is
/// split before each stream's [`LayerSelector`](crate::simulcast::LayerSelector) picks a layer.
pub struct Allocator;

impl Allocator {
    /// Split `budget_bps` across `stream_count` streams, returning the per-stream budget.
    ///
    /// TODO(V4): reserve a small headroom margin (don't allocate 100% — leave room to probe),
    /// then divide the rest. A priority scheme (camera over screen-share, or equal split) is
    /// yours to choose and document; the single-stream case just returns `budget − margin`.
    pub fn split(budget_bps: u32, stream_count: usize) -> Vec<u32> {
        let _ = (budget_bps, stream_count);
        todo!("V4: reserve headroom, divide the budget across the subscriber's streams")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove the controller:
    //   - `backs_off_on_rising_delay`: a batch of samples whose arrival spacing grows lowers
    //     the estimate; a flat batch lets it climb;
    //   - `backs_off_on_loss` / `recovers_on_clear`: 20% loss lowers the estimate, 0% loss
    //     climbs it back toward max;
    //   - `stays_clamped`: no sequence of hostile/garbage feedback drives it below min, above
    //     max, negative, or NaN;
    //   - `allocator_reserves_headroom`: `split` never hands out the full budget and sums to
    //     ≤ budget across streams.
}
