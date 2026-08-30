"""V2 — The rollup engine: streaming windowed aggregation + a percentile sketch.

Fold the point stream into per-series, per-window `Aggregate`s *online* (single
pass, bounded memory) and emit each window as a `RollupRow` when it closes. This
is the bridge between an unqueryable firehose of raw points and the handful of
summarized rows a dashboard can read in milliseconds.

Two things make this more than a dict (see SPEC V2):

1. **Percentiles don't average.** To answer p99 — and to roll 1m windows up into
   5m/1h — you must carry a *mergeable sketch of the distribution*, not a
   precomputed percentile. Build one: a fixed-bucket histogram or a t-digest.
2. **Windows must close.** Points arrive late and out of order; a **watermark**
   decides when a window is done and gets flushed, and what happens to a point
   that shows up after its window already flushed.

Concurrency: this object is owned by the single consumer task in `pipeline.py`
and is deliberately **not** async. One writer means no locks and no `await`
inside a read-modify-write — which is the correct shape here, and knowing *why*
you were allowed to skip the lock is the point. If you ever fan the consumer out
across tasks, this assumption is the first thing that breaks.

Scaffold state: the engine is constructed and driven by the pipeline, but every
real operation raises. `RUN_CONSUMER=true` makes the loop hit them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .model import Aggregate, MetricPoint, RollupRow, WindowKey

__all__ = ["Rollup"]


class Rollup:
    """Accumulates open windows and flushes them as their watermark passes.

    Holds one `Aggregate` per `(series, window)` currently open. The size of
    `_open` *is* your live memory footprint — bounded by active cardinality x
    open windows — and watching it is the OOM canary (SPEC: observability).
    """

    def __init__(self, window: timedelta, grace: timedelta) -> None:
        if window <= timedelta(0):
            raise ValueError("window width must be > 0")
        self._window = window
        """Tumbling-window width. Every point snaps to a multiple of this."""
        self._grace = grace
        """How long to wait past a window's end before flushing it, to absorb
        late / out-of-order points (the watermark grace period)."""
        self._open: dict[WindowKey, Aggregate] = {}
        """Open windows keyed by `(series_id, window_start)`."""

    def ingest(self, point: MetricPoint) -> None:
        """Fold one point into its window's running aggregate."""
        # TODO(V2): the online aggregation step.
        #   - snap `point.timestamp` DOWN to a multiple of `self._window` to get
        #     `window_start`, then build the `WindowKey` (needs the V1
        #     fingerprint). Integer arithmetic on the epoch is the clean way:
        #     `epoch = point.timestamp.timestamp()`, floor-divide by the window
        #     width in seconds, multiply back, and rebuild with
        #     `datetime.fromtimestamp(..., tz=UTC)`. Do NOT reach for
        #     `.replace(minute=...)` — it only works for windows that happen to
        #     divide an hour, and it silently misbehaves for 90s or 7m windows.
        #   - upsert the `Aggregate` (`self._open.get(key)` then insert, or
        #     `setdefault`): bump count, add to sum, min/max, set last, and FEED
        #     THE VALUE INTO THE SKETCH. Never keep the raw values — a list per
        #     window is just deferring the memory blow-up.
        #   - if the point is older than the current watermark (its window has
        #     already flushed) it is LATE: drop it and count it, or re-open —
        #     your policy (SPEC V2). Don't silently grow the dict forever.
        raise NotImplementedError("V2: snap to window + update the online aggregate (incl. sketch)")

    def flush_ready(self, now: datetime) -> list[RollupRow]:
        """Flush every window whose watermark has passed, removing it from the
        open set and returning it as a finished row.

        Called on a timer by the pipeline loop; the returned rows go to the sink
        (V3) and the SSE fan-out (V4).
        """
        # TODO(V2): the watermark flush.
        #   - a window is ready when `window_start + window + grace <= now`.
        #   - collect the ready keys FIRST, then delete — mutating a dict while
        #     iterating it raises RuntimeError in Python, and the workaround
        #     (`list(self._open.items())`) copies every key on every tick, which
        #     at 100k live series is the tick that shows up in your p99. If that
        #     copy hurts, the fix is a structure that keeps windows ordered by
        #     start time (a heap of window starts, or a dict-of-dicts keyed by
        #     window first) so the flush touches only what is actually ready.
        #   - for each ready key, pop it and turn its `Aggregate` into a
        #     `RollupRow`, asking the sketch for p50/p99.
        #   - returning them here is what bounds memory: a flushed window is gone
        #     from `self._open`.
        raise NotImplementedError("V2: flush windows whose watermark has passed into RollupRows")

    def drain_all(self) -> list[RollupRow]:
        """Emit *every* open window regardless of watermark, and clear the map.

        For graceful shutdown, so a clean stop flushes partial windows instead of
        dropping them (SPEC: graceful shutdown).
        """
        # TODO(V2): same Aggregate -> RollupRow conversion as `flush_ready` —
        # factor that out into one helper rather than writing it twice.
        raise NotImplementedError("V2: drain all open windows (used on graceful shutdown)")

    @property
    def open_windows(self) -> int:
        """Number of windows currently held in memory — export this as a gauge.

        Wired (not a todo) on purpose: it is the OOM canary, and you want it
        readable from the very first window you open.
        """
        return len(self._open)
