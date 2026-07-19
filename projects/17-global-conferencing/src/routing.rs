//! V3 — Cross-region simulcast routing. `src/routing.rs`.
//!
//! Inside one region, layer selection is **per subscriber** (project 15's `LayerSelector`). Across
//! the cascade it's a different question, one tier up: which simulcast **layers** does each relay
//! leg (V2) carry? Frankfurt has a fibre viewer wanting the **high** layer and a mobile viewer
//! wanting the **low** layer — so the backbone leg to Frankfurt must carry **both** (the *union*):
//! carrying only one starves somebody, carrying all three wastes the layer nobody there watches.
//! So each remote SFU **aggregates** its local subscribers' demand into a per-region layer set,
//! and the origin forwards down each leg exactly that **union of downstream demand — no more**.
//!
//! The keyframe subtlety from p15 lifts to the cascade: when a region **newly** demands a higher
//! layer, that demand propagates upstream and the origin must request a **keyframe (PLI/FIR)** from
//! the publisher on that layer — **once** per up-switch, not once per packet — before it can flow
//! on the leg. And demand needs **hysteresis** so a viewer flapping around a layer boundary doesn't
//! thrash the backbone leg.
//!
//! Scaffold state: [`LayerRouter::new`] and the read-only [`snapshot`](LayerRouter::snapshot) are
//! wired so `/status` shows the (empty) per-leg layer sets. Aggregating demand, computing the
//! per-leg union, and gating up-switches on a keyframe are the V3 `todo!()` worklist.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::RwLock;

use serde::Serialize;

use crate::error::Result;

/// A simulcast layer id: the index of an encoding in a publisher's announced ladder (0 = lowest).
/// The same ids project 15's publisher announces and its `LayerSelector` picks among.
pub type LayerId = u8;

/// A track that layers are routed for: a publisher's stream, keyed by its `(room, publisher)`.
pub type TrackKey = (String, u64);

/// The set of layers a backbone leg carries for one track — the union of that region's demand.
pub type LayerSet = BTreeSet<LayerId>;

/// A keyframe request the origin must send upstream to a publisher when a region first demands a
/// layer higher than a leg currently carries (V3 → project 15's PLI/FIR mechanism).
#[derive(Clone, Debug, Serialize)]
pub struct KeyframeRequest {
    /// Which publisher/track to request a keyframe from.
    pub publisher: u64,
    /// The (higher) layer the keyframe is needed on before the leg can carry it.
    pub layer: LayerId,
}

/// A snapshot row for `/status`: which layers a leg carries for a track, by region.
#[derive(Clone, Debug, Serialize)]
pub struct LegDemand {
    pub region: String,
    pub publisher: u64,
    pub layers: Vec<LayerId>,
}

/// Config for the layer router, read from env in `main`.
pub struct RoutingConfig {
    /// Hysteresis hold: how many recompute ticks a layer must stay un-demanded before it's
    /// dropped from a leg (damps a flapping subscriber). Documented in the design doc.
    pub hysteresis_ticks: u32,
}

/// Per-region, per-track aggregated demand + the layer set currently on each leg.
#[derive(Default)]
struct Inner {
    /// (track) → region → the layer set that region's leg currently carries.
    legs: HashMap<TrackKey, BTreeMap<String, LayerSet>>,
}

/// The cross-region layer router: aggregates demand and decides each leg's carried layer set.
pub struct LayerRouter {
    cfg: RoutingConfig,
    inner: RwLock<Inner>,
}

impl LayerRouter {
    /// Build the router. Wiring only.
    pub fn new(cfg: RoutingConfig) -> Self {
        Self {
            cfg,
            inner: RwLock::new(Inner::default()),
        }
    }

    pub fn config(&self) -> &RoutingConfig {
        &self.cfg
    }

    /// A snapshot of every leg's carried layer set (for `/status`).
    pub fn snapshot(&self) -> Vec<LegDemand> {
        let inner = self.inner.read().expect("routing lock");
        let mut out = Vec::new();
        for ((room, publisher), by_region) in inner.legs.iter() {
            let _ = room;
            for (region, layers) in by_region {
                out.push(LegDemand {
                    region: region.clone(),
                    publisher: *publisher,
                    layers: layers.iter().copied().collect(),
                });
            }
        }
        out
    }

    // ---- V3 worklist: aggregate demand · union per leg · gate up-switches -----------------

    /// TODO(V3): Aggregate `region`'s **local** subscribers' selected layers (project 15's
    /// per-subscriber choice) into that region's demand for a track — the union of what its locals
    /// need. Called as locals join/leave/change layer. Returns the region's demanded [`LayerSet`].
    pub fn aggregate_local_demand(
        &self,
        track: &TrackKey,
        region: &str,
        subscriber_layers: &[LayerId],
    ) -> LayerSet {
        let _ = (track, region, subscriber_layers);
        todo!("V3: fold local subscribers' selected layers into this region's demanded set")
    }

    /// TODO(V3): Recompute the layer set the leg to `region` should carry for `track` from that
    /// region's aggregated demand, applying hysteresis so a flapping subscriber doesn't toggle the
    /// leg every tick. Returns any [`KeyframeRequest`]s (a *newly* demanded higher layer needs one
    /// upstream PLI/FIR before it can flow — exactly one, not one per packet). Updates the leg set.
    pub fn recompute_leg(
        &self,
        track: &TrackKey,
        region: &str,
        demand: &LayerSet,
    ) -> Result<Vec<KeyframeRequest>> {
        let _ = (track, region, demand, self.cfg.hysteresis_ticks);
        todo!(
            "V3: set leg = union of demand (with hysteresis); emit one keyframe req per up-switch"
        )
    }

    /// TODO(V3): Whether a packet on `layer` of `track` should be relayed to `region` — true iff
    /// that layer is in the leg's current carried set. Lets V2's `relay_out` forward only the union
    /// (deselected layers are not sent across the backbone).
    pub fn leg_carries(&self, track: &TrackKey, region: &str, layer: LayerId) -> bool {
        let _ = (track, region, layer);
        todo!("V3: report whether this leg currently carries this layer")
    }
}
