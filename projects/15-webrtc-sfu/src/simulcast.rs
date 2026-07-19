//! V3 — Simulcast layer selection: give each subscriber the quality *their* link can take.
//!
//! One subscriber is on fibre, another on a train. If the publisher sends a single 2 Mbps
//! stream, the SFU can only choose "melt the train" or "starve the fibre". **Simulcast**
//! fixes this at the source: the publisher encodes the *same video* two or three times at
//! different resolutions/bitrates and sends all of them at once, each as its **own SSRC/RID**
//! (a low ~150 kbps, a mid ~500 kbps, a high ~2 Mbps layer). The SFU then, **per subscriber**,
//! forwards exactly **one** of those layers — the highest one that fits that subscriber's
//! estimated downlink bandwidth (V4) — and can switch layers as the estimate moves. This is
//! the SFU's superpower over a naive relay: it adapts quality *without decoding a pixel*.
//!
//! Two things make it subtle. First, you can only **switch up** to a higher layer at a
//! packet the decoder can start from cleanly — a **keyframe** (all other frames reference
//! earlier ones the subscriber never received). So switching up means: send a **keyframe
//! request (PLI/FIR)** upstream to the publisher, then start forwarding the new layer from
//! its next keyframe — until then keep sending the old layer. Second, the switch must be
//! invisible downstream: the subscriber sees one continuous SSRC/seq/ts line (that's V2's
//! rewriter), so a layer switch is a change of *which origin feeds the rewriter*, not a
//! change the subscriber's jitter buffer ever notices.

/// One simulcast encoding the publisher is sending.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SimulcastLayer {
    /// RTP stream id ("q"/"h"/"f", low→high) — the publisher's label for this encoding.
    pub rid: String,
    /// The SSRC this layer's RTP packets carry (how the SFU tells layers apart on the wire).
    pub ssrc: u32,
    /// Nominal bitrate of this layer, bits/sec — what it costs a subscriber to receive it.
    pub bitrate_bps: u32,
}

/// A layer-selection decision for one origin packet.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    /// Forward this packet to the subscriber (it belongs to the currently selected layer).
    Forward,
    /// Drop it (belongs to a layer this subscriber isn't currently receiving) — the rewriter
    /// still needs to `skip` so the outbound sequence stays gapless.
    Drop,
}

/// Per-subscriber layer selector: holds the available layers, the currently forwarded one,
/// the pending target (set by the bandwidth estimator), and whether a keyframe is owed
/// before an upward switch can take effect.
pub struct LayerSelector {
    layers: Vec<SimulcastLayer>,
    // TODO(V3): the state a keyframe-gated switch needs, e.g. the currently-forwarded layer
    // index, a pending target index chosen from the budget, and a `waiting_for_keyframe`
    // flag so an up-switch only commits at a decodable boundary.
    _target_bps: u32,
}

impl LayerSelector {
    /// Build a selector over a publisher's advertised layers (sorted low→high by the caller
    /// or here). Starts on the lowest layer — safe until an estimate says otherwise.
    pub fn new(layers: Vec<SimulcastLayer>) -> Self {
        Self {
            layers,
            _target_bps: 0,
        }
    }

    /// Feed the current downlink budget (bits/sec) from the estimator (V4). Chooses the
    /// **highest layer whose bitrate ≤ budget** as the *target*; committing to it may still
    /// wait for a keyframe if it's an up-switch.
    ///
    /// TODO(V3): pick the target layer index from `budget_bps` (highest that fits, never
    /// below the lowest); if the target is **higher** than the current layer, set the
    /// keyframe-owed flag (an up-switch needs a keyframe); a **downward** switch can take
    /// effect immediately (lower layers are always safe to drop down to).
    pub fn set_budget(&mut self, budget_bps: u32) {
        let _ = (budget_bps, &self.layers, &mut self._target_bps);
        todo!("V3: choose target layer from the budget; flag a keyframe-owed on an up-switch")
    }

    /// True while an up-switch is pending a keyframe — the core sends a PLI/FIR upstream and
    /// clears it once [`on_packet`](Self::on_packet) sees the keyframe on the target layer.
    ///
    /// TODO(V3): report whether a keyframe request is currently owed (so the wired core sends
    /// exactly one PLI per pending switch, not one per packet).
    pub fn wants_keyframe(&self) -> bool {
        todo!("V3: true iff an up-switch is waiting for a keyframe on the target layer")
    }

    /// Decide whether an origin packet (from layer `ssrc`, `is_keyframe`) should be forwarded
    /// to this subscriber, committing a pending up-switch when its keyframe arrives.
    ///
    /// TODO(V3): if `ssrc` is the currently forwarded layer → [`Decision::Forward`]. If it's
    /// the pending target layer **and** `is_keyframe` → commit the switch (now forward from
    /// here on, clear the keyframe-owed flag) and `Forward`. Any other layer → [`Decision::
    /// Drop`]. This is what makes a switch land only at a boundary the subscriber's decoder
    /// can actually start from.
    pub fn on_packet(&mut self, ssrc: u32, is_keyframe: bool) -> Decision {
        let _ = (ssrc, is_keyframe, &self.layers, &mut self._target_bps);
        todo!("V3: forward the selected layer; commit an up-switch only on the target's keyframe")
    }

    /// Bitrate currently selected for this subscriber (for the SELECTED_BITRATE gauge).
    ///
    /// TODO(V3): the `bitrate_bps` of the currently forwarded layer.
    pub fn selected_bitrate(&self) -> u32 {
        todo!("V3: report the bitrate of the currently forwarded layer")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V3): prove selection:
    //   - `picks_highest_fitting_layer`: given low/mid/high layers, a budget just above mid
    //     selects mid, a budget below low still selects low (never nothing);
    //   - `up_switch_waits_for_keyframe`: after a budget rise, `wants_keyframe()` is true and
    //     `on_packet` keeps forwarding the *old* layer until a keyframe arrives on the target,
    //     then commits — and `wants_keyframe()` clears;
    //   - `down_switch_is_immediate`: a budget drop switches down without waiting for a keyframe;
    //   - `deselected_layer_is_dropped`: packets from non-selected layers return `Drop`.
}
