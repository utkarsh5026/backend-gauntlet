"""V3 — Simulcast layer selection: give each subscriber the quality *their* link can take.

One subscriber is on fibre, another on a train. If the publisher sends a single
2 Mbps stream, the SFU's only choices are "melt the train" and "starve the
fibre". **Simulcast** fixes this at the source: the publisher encodes the *same
video* two or three times at different resolutions and bitrates and sends all of
them at once, each as its own SSRC/RID — a low (~150 kbps), a mid (~500 kbps)
and a high (~2 Mbps) layer. The SFU then, **per subscriber**, forwards exactly
**one** of them: the highest that fits that subscriber's estimated downlink
budget (V4), switching as the estimate moves. This is the SFU's superpower over
a naive relay — it adapts quality *without decoding a pixel*.

Two things make it subtle.

**You can only switch up at a keyframe.** Every other frame references earlier
frames the subscriber never received, so starting mid-GoP gives them a decoder
full of green smear. An up-switch therefore means: ask the publisher for a
keyframe (PLI/FIR), keep forwarding the *old* layer, and start forwarding the
new one only from its next keyframe. A **down**-switch is always safe
immediately — you already have the lower layer's frames.

**The switch must be invisible downstream.** The subscriber sees one continuous
SSRC/seq/ts line, which is V2's rewriter, so a layer switch is a change of
*which origin feeds the rewriter* — never something the subscriber's jitter
buffer notices. V3 decides; V2 hides.

## The flag that must clear

`wants_keyframe` is read on the packet path, and the wired core sends one PLI
each time it is true. If committing the switch does not clear it, you send a PLI
per *packet* rather than per switch — a keyframe request storm upstream that
looks, from the publisher's side, exactly like a subscriber whose link has
collapsed. `sfu_keyframe_requests_total` tracking the packet rate instead of the
switch rate is that bug, visible on a graph.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Decision", "LayerSelector", "SimulcastLayer"]


@dataclass(frozen=True, slots=True)
class SimulcastLayer:
    """One simulcast encoding the publisher is sending."""

    rid: str
    """RTP stream id ("q"/"h"/"f", low -> high) — the publisher's label."""

    ssrc: int
    """The SSRC this layer's packets carry — how the SFU tells layers apart."""

    bitrate_bps: int
    """Nominal bitrate, bits/sec — what it costs a subscriber to receive it."""


class Decision(StrEnum):
    """A layer-selection decision for one origin packet."""

    FORWARD = "forward"
    """It belongs to the currently selected layer — send it on."""

    DROP = "drop"
    """It belongs to a layer this subscriber is not receiving. The rewriter
    still needs a `skip()` so the outbound sequence stays gapless."""


class LayerSelector:
    """Per-subscriber layer selector.

    Holds the publisher's available layers, the one currently forwarded, the
    pending target chosen from the bandwidth estimate, and whether a keyframe is
    owed before an upward switch may take effect.
    """

    __slots__ = ("_state", "layers")

    def __init__(self, layers: Iterable[SimulcastLayer]) -> None:
        self.layers = sorted(layers, key=lambda layer: layer.bitrate_bps)
        """Sorted low -> high once, here, so every lookup downstream is a scan
        over an ordered list and "the highest that fits" is a `bisect` away
        rather than a `max` over a filter. A publisher may announce layers in
        any order; nothing downstream should have to care."""

        # TODO(V3): the state a keyframe-gated switch needs. Roughly: the index
        # of the layer currently forwarded, the index of a pending target chosen
        # from the budget, and a flag recording that an up-switch is waiting for
        # a keyframe on that target. Start on the lowest layer — safe until an
        # estimate says otherwise, and never "nothing".
        self._state: None = None

    def set_budget(self, budget_bps: int) -> None:
        """Feed the current downlink budget (bits/sec) from the estimator (V4).

        TODO(V3): choose the target layer — the highest whose `bitrate_bps` is
        at or below `budget_bps`, and the lowest layer when nothing fits (the
        SFU never forwards *nothing* while a layer exists). `bisect.bisect_right`
        over `[layer.bitrate_bps for layer in self.layers]` is the direct shape;
        precompute that list in `__init__` rather than rebuilding it per call,
        because this runs on every receiver report.

        If the target is **higher** than the layer currently forwarded, set the
        keyframe-owed flag — an up-switch needs a decodable boundary. A
        **downward** move takes effect immediately.

        Two policy decisions the SPEC grades and does not make for you: whether
        you leave a margin below the budget rather than selecting right up to
        it, and what hysteresis stops a subscriber hovering on a boundary from
        flapping between two layers forever. Both belong in
        `docs/15-design.md`; both are visible in `sfu_layer_switches_total`.
        """
        raise NotImplementedError("V3: choose the target layer; flag keyframe-owed on an up-switch")

    @property
    def wants_keyframe(self) -> bool:
        """True while an up-switch is pending a keyframe on the target layer.

        The wired core reads this after each packet and sends one PLI/FIR
        upstream while it holds.

        TODO(V3): report the keyframe-owed flag. It must clear when the switch
        commits — see the module docstring on why.
        """
        raise NotImplementedError("V3: true iff an up-switch is waiting for a keyframe")

    def on_packet(self, ssrc: int, is_keyframe: bool) -> Decision:
        """Decide whether an origin packet should go to this subscriber.

        TODO(V3): if `ssrc` is the currently forwarded layer, `Decision.FORWARD`.
        If it is the pending target **and** `is_keyframe`, commit the switch —
        forward from here on, clear the keyframe-owed flag — and `FORWARD`.
        Anything else is `Decision.DROP`.

        That single "and `is_keyframe`" is what makes a switch land only at a
        boundary the subscriber's decoder can actually start from, and it is the
        entire difference between a clean quality change and two seconds of
        garbage.
        """
        raise NotImplementedError(
            "V3: forward the selected layer; commit up-switches on a keyframe"
        )

    @property
    def selected_bitrate(self) -> int:
        """Bitrate of the layer currently forwarded (for `SELECTED_BITRATE_BPS`).

        TODO(V3): the `bitrate_bps` of the currently forwarded layer, or 0 when
        the publisher announced no layers at all.
        """
        raise NotImplementedError("V3: report the currently forwarded layer's bitrate")
