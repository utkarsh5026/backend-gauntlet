"""Shared time-series types: the data model the whole pipeline is keyed on.

These are the values the verticals pass around — `parse` turns a wire line into
`MetricPoint`s (V1), `rollup` folds points into an `Aggregate` per `WindowKey`
(V2), the sink writes `RollupRow`s (V3), and the SSE feed streams them (V4). The
types are deliberately plain data; the *interesting* operations on them (the
series fingerprint, the online aggregation, the percentile sketch) live in the
vertical modules, not here.

Two Python-specific choices worth understanding before you build on them:

* The hot-path types are **frozen slotted dataclasses**, not pydantic models.
  There is one `MetricPoint` per field per line — at a firehose that is millions
  of short-lived objects, and pydantic's validation cost per object would show up
  in the ingest number the boss fight grades. Validation belongs at the *edge*
  (the parser, once) rather than on every internal hand-off.
* `Series.tags` is a **tuple of pairs, not a dict**. A dict is unhashable and its
  order is insertion order — both fatal for something whose whole job is to be a
  stable identity. A sorted tuple is hashable, comparable, and canonical.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

__all__ = [
    "Aggregate",
    "MetricPoint",
    "RollupRow",
    "Series",
    "SeriesId",
    "WindowKey",
]

# Stable identity of a series = a fingerprint of its measurement + tag set.
#
# Two points belong to the same series iff their measurement and their *exact*
# set of tags match. The fingerprint is computed over the tags **sorted by key**
# so `a=1,b=2` and `b=2,a=1` collapse to one series — see `parse.py` (V1) for
# where it is derived.
type SeriesId = int


@dataclass(frozen=True, slots=True)
class Series:
    """A measurement plus its tag set — the dimensions you filter and group by.

    Invariant the parser must uphold: `tags` is sorted by key (canonical form),
    so the `SeriesId` is stable regardless of the order tags arrived in.
    """

    measurement: str
    """What is being measured, e.g. `cpu`, `http_requests`."""

    tags: tuple[tuple[str, str], ...]
    """`(key, value)` dimensions, **sorted by key**. Each distinct tag set is a
    new series — this is where cardinality comes from (see SPEC V1)."""


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """A single observation: one numeric value for one series at one instant.

    A wire line with several fields (`usage=0.91,sys=0.12`) parses into several
    points — one per field — each its own series (the field name folds into the
    measurement or into a reserved tag; that is a V1 modelling choice).
    """

    series: Series
    value: float
    timestamp: datetime
    """When the observation happened, timezone-aware UTC. Defaults to ingest time
    when the wire line omits a timestamp (a V1 decision).

    Naive datetimes are banned on purpose: `datetime.now()` without a tzinfo
    compares unequal to everything the parser produces from a unix timestamp, and
    the bug surfaces hours later as points in the wrong window."""


@dataclass(frozen=True, slots=True)
class WindowKey:
    """The bucket a point falls into: a series, pinned to a tumbling window start.

    `window_start` is the point's timestamp snapped *down* to a multiple of the
    window width, so every point in `[start, start + width)` shares one key.
    Frozen (therefore hashable) because this is a dict key on the hot path.
    """

    series_id: SeriesId
    window_start: datetime


@dataclass(slots=True)
class Aggregate:
    """The running summary of every point in one window of one series (V2).

    `count/sum/min/max/last` update in a single pass — mutable and slotted
    because there is one of these per open window and it is updated per point.

    The percentile sketch is the hard part and is intentionally *not* a field
    here yet: building a mergeable sketch and threading it through is the V2
    challenge, so it is left for you to add (`p50`/`p99` on `RollupRow` are where
    it surfaces).
    """

    count: int = 0
    sum: float = 0.0
    min: float = float("inf")
    max: float = float("-inf")
    last: float = 0.0
    """The most recently seen value (by arrival) in the window."""

    # TODO(V2): add your mergeable percentile sketch here (a fixed-bucket
    # histogram, or a t-digest) so a window can answer p50/p95/p99 and two
    # windows can be merged for coarser rollups (1m -> 5m -> 1h). You cannot
    # store the percentile itself — percentiles don't average. Store the
    # *distribution*.
    #
    # Sizing is the whole design: this object exists once per (series, window),
    # so a sketch that costs a few hundred bytes at 100k live series is tens of
    # megabytes of steady-state heap. A `list[int]` of bucket counts costs ~8
    # bytes *per slot plus* a boxed int per distinct value; `array("q", ...)`
    # from the stdlib is a flat C array of the same counts with no per-element
    # object. Measure both — that difference is a SPEC criterion, not trivia.


class RollupRow(BaseModel):
    """A finished, flushed rollup — written to ClickHouse (V3) and pushed to
    dashboards over SSE (V4). One row per `(series, window)`.

    This one *is* a pydantic model, unlike the hot-path types above: it crosses
    process boundaries (JSON on the wire, columns in the store), so it wants a
    schema, and there are orders of magnitude fewer of them than there are points.
    """

    series_id: SeriesId
    measurement: str
    window_start: datetime
    """Start of the window this row summarizes."""
    window_secs: int
    """Window width in seconds (the rollup resolution: 1, 60, 3600, …)."""
    count: int
    sum: float
    min: float
    max: float
    p50: float
    """Quantiles drawn from the V2 sketch. Zero until the sketch exists."""
    p99: float
