"""V3 — Cross-region simulcast routing. `src/global_conferencing/routing.py`.

Inside one region, layer selection is **per subscriber** (project 15's
`LayerSelector`). Across the cascade it is a different question, one tier up:
which simulcast **layers** does each relay leg (V2) carry? Frankfurt has a fibre
viewer wanting the **high** layer and a mobile viewer wanting the **low** layer —
so the backbone leg to Frankfurt must carry **both**, the *union*: carrying only
one starves somebody, carrying all three sends the mid layer across an ocean for
nobody. Each remote SFU **aggregates** its local subscribers' demand into a
per-region layer set, and the origin forwards down each leg exactly that
**union of downstream demand — no more**.

The keyframe subtlety from project 15 lifts to the cascade. When a region
**newly** demands a higher layer, the origin must request a **keyframe
(PLI/FIR)** from the publisher on that layer — **once** per up-switch, not once
per packet — and keep sending the current set until it arrives, because a layer
that starts flowing mid-GoP is undecodable for every viewer downstream. And
demand needs **hysteresis**, so one viewer hovering on a layer boundary does not
toggle a transcontinental leg on and off.

## Why frozensets

A leg's carried layers are a *set* in the mathematical sense, and the operations
this module is graded on are set operations: the union of a region's demand, the
difference between what is demanded and what is carried (the newly-demanded
layers, which are exactly the ones owed a keyframe), the difference the other
way (candidates to drop, subject to hysteresis). `frozenset` gives you `|`, `-`
and `<=` directly and is hashable, so a leg's set can be compared, logged or used
as a key without a defensive copy.

Scaffold state: construction and the `/status` snapshot are wired. Aggregating
demand, computing each leg's set, and gating up-switches on a keyframe are the V3
worklist. Everything here is synchronous on purpose — `leg_carries` runs per
relayed packet, and an `await` on that path is a scheduling point per packet.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel

from .ids import LayerId, TrackKey

__all__ = ["KeyframeRequest", "LayerRouter", "LayerSet", "LegLayers", "RoutingConfig"]

type LayerSet = frozenset[LayerId]
"""The layers a leg carries for one track — the union of that region's demand."""


@dataclass(frozen=True, slots=True)
class KeyframeRequest:
    """A keyframe the origin must request upstream before a leg can carry `layer`.

    Handed to project 15's PLI/FIR path. One per up-switch, never one per packet.
    """

    track: TrackKey
    layer: LayerId


class LegLayers(BaseModel):
    """A `/status` row: which layers one leg carries for one track."""

    room_id: str
    publisher: int
    region: str
    layers: list[int]


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    hysteresis_ticks: int
    """Recompute ticks a layer must stay un-demanded before it is dropped from a
    leg. The damping margin the SPEC asks you to document."""


class LayerRouter:
    """Aggregates demand per region and decides each backbone leg's layer set."""

    def __init__(self, config: RoutingConfig) -> None:
        self.config = config
        self._legs: dict[TrackKey, dict[str, LayerSet]] = {}
        """track → region → the layer set that region's leg currently carries."""

        # TODO(V3): the state hysteresis and keyframe gating need. Roughly: per
        # (track, region, layer), how many ticks it has gone un-demanded; and per
        # track, which layers are owed a keyframe before they may flow. A
        # `dict[LayerId, int]` of idle ticks is one shape of the first; a
        # `set[LayerId]` of pending layers is one shape of the second.

    def snapshot(self) -> list[LegLayers]:
        """Every leg's carried layer set (for `/status`)."""
        return [
            LegLayers(room_id=room_id, publisher=publisher, region=region, layers=sorted(layers))
            for (room_id, publisher), by_region in sorted(self._legs.items())
            for region, layers in sorted(by_region.items())
        ]

    # ---- V3 worklist: aggregate · union per leg · gate up-switches ---------

    def aggregate_local_demand(
        self,
        track: TrackKey,
        region: str,
        subscriber_layers: Iterable[LayerId],
    ) -> LayerSet:
        """Fold `region`'s local subscribers' selected layers into its demand for `track`.

        TODO(V3): the input is project 15's per-subscriber choice, one entry per
        local subscriber (a recorder counts — V4 is a subscriber too). The output
        is the set of layers somebody in that region needs. Called whenever a local
        subscriber joins, leaves or changes layer, so it must be correct for an
        empty region too (the demand of nobody is nothing, and that is what lets a
        leg shrink).
        """
        raise NotImplementedError(
            "V3: fold local subscribers' selected layers into this region's demanded set"
        )

    def recompute_leg(
        self,
        track: TrackKey,
        region: str,
        demand: LayerSet,
    ) -> list[KeyframeRequest]:
        """Update the set the leg to `region` carries for `track`; return keyframes owed.

        TODO(V3): the leg converges on `demand`, with two asymmetries.

        * **Up** is gated: a layer in `demand` the leg does not carry yet is owed
          a keyframe. Return one `KeyframeRequest` for it the *first* time it is
          seen — not again on the next recompute while it is still pending — and
          do not add it to the carried set until `on_keyframe` says it may flow.
        * **Down** is damped: a layer the leg carries that is no longer demanded
          leaves only after `config.hysteresis_ticks` consecutive recomputes
          without demand.

        Bump `KEYFRAME_REQUESTS_TOTAL` per request returned, `DEMAND_CHANGES_TOTAL`
        when the carried set actually changes, and set `LEG_LAYERS{peer=region}`.
        """
        raise NotImplementedError(
            "V3: set leg = union of demand (with hysteresis); emit one keyframe req per up-switch"
        )

    def on_keyframe(self, track: TrackKey, layer: LayerId) -> None:
        """A keyframe arrived from the publisher on `layer` of `track`.

        TODO(V3): every leg waiting on this layer may now carry it — add it to
        those legs' sets and clear the keyframe-owed state. If that state does not
        clear, the next recompute asks again, and `conf_keyframe_requests_total`
        climbs at the packet rate instead of the switch rate.
        """
        raise NotImplementedError("V3: commit pending up-switches for this layer, clear owed flag")

    def leg_carries(self, track: TrackKey, region: str, layer: LayerId) -> bool:
        """Whether a packet on `layer` of `track` should be relayed to `region`.

        TODO(V3): true iff `layer` is in that leg's *current* carried set (not the
        pending one). This is the check V2's `relay_out` asks per packet, so it is
        a dictionary lookup and a set membership test, and nothing more.
        """
        raise NotImplementedError("V3: report whether this leg currently carries this layer")
