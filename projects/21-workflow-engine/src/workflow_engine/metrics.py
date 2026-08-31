"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register themselves into the default registry at
import time, and `common_telemetry.metrics_routes()` renders that same registry
at `/metrics` — so declaring a metric here is all the wiring there is. That
replaces the Rust side's install-a-global-recorder step entirely: there is no
`install()` to call and no ordering constraint against telemetry init.

The series the SPEC grades:

* `WORKFLOW_TASKS_TOTAL` / `ACTIVITY_TASKS_TOTAL` — tasks dispatched to workers.
* `REPLAYS_TOTAL` — labelled `sticky = hit|miss`; the sticky hit ratio (V5).
* `EVENTS_REPLAYED` — a histogram, also labelled `sticky`, because the hit ratio
  alone doesn't prove stickiness *paid*. "A hit replays ≥5× fewer events than a
  miss" is a claim about two distributions, and this is the only shape that can
  answer it after the fact.
* `TIMERS_FIRED_TOTAL` — durable timers fired (V3).
* `EXECUTIONS_COMPLETED_TOTAL` — labelled `outcome = completed|failed`.
* `TASK_QUEUE_DEPTH` — pending tasks not yet claimed; the backpressure signal.

Note what is *not* here: a `sticky_hit_ratio` gauge. Counters per outcome, never
a pre-computed ratio — a ratio can't be aggregated across replicas or
re-windowed after the fact; two counters can, and Prometheus divides at query
time.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "ACTIVITY_TASKS_TOTAL",
    "STICKY_PINS",
    "DISPATCH_LATENCY_SECONDS",
    "EVENTS_REPLAYED",
    "EXECUTIONS_COMPLETED_TOTAL",
    "NONDETERMINISM_TOTAL",
    "REPLAYS_TOTAL",
    "TASK_QUEUE_DEPTH",
    "TIMERS_FIRED_TOTAL",
    "WORKFLOW_TASKS_TOTAL",
]

# ---- Counters (rates) ------------------------------------------------------

WORKFLOW_TASKS_TOTAL = Counter(
    "workflow_workflow_tasks_total",
    "Workflow tasks dispatched to workers (each is one decision the workflow makes)",
    ["task_queue"],
)

ACTIVITY_TASKS_TOTAL = Counter(
    "workflow_activity_tasks_total",
    "Activity tasks dispatched to workers",
    ["task_queue"],
)

REPLAYS_TOTAL = Counter(
    "workflow_replays_total",
    "Workflow replays; hit ratio = hit / (hit + miss)",
    ["sticky"],
)

TIMERS_FIRED_TOTAL = Counter(
    "workflow_timers_fired_total",
    "Durable timers fired into history (V3)",
)

EXECUTIONS_COMPLETED_TOTAL = Counter(
    "workflow_executions_completed_total",
    "Executions that reached a terminal state",
    ["outcome"],
)

NONDETERMINISM_TOTAL = Counter(
    "workflow_nondeterminism_total",
    "Workflow tasks rejected because the worker's commands diverged from history",
)

# ---- Gauges (current state) ------------------------------------------------

TASK_QUEUE_DEPTH = Gauge(
    "workflow_task_queue_depth",
    "Pending tasks not yet claimed by a worker",
    ["task_queue", "kind"],
)

STICKY_PINS = Gauge(
    "workflow_sticky_pins",
    "Executions currently pinned to a worker (V5)",
)
"""Wired in `routes.create_admin_app` with `set_function`, not `set`.

A callback gauge is read at *scrape* time from the live object, so there is no
update call to forget on some path that drops a pin — and no risk of the number
drifting from reality while looking authoritative, which is worse than not having
it. Use `set_function` for anything you can simply *ask*; use `set`/`inc` for
things that happen."""

# ---- Histograms (distributions) --------------------------------------------

EVENTS_REPLAYED = Histogram(
    "workflow_events_replayed",
    "History events shipped to a worker per workflow task",
    ["sticky"],
    # A sticky hit should land in the single digits and a miss should climb with
    # the execution's age, so the buckets have to span both — a default
    # latency-shaped bucket set would put every value in `+Inf`.
    buckets=(1, 2, 5, 10, 25, 50, 100, 250, 1000, 5000),
)

DISPATCH_LATENCY_SECONDS = Histogram(
    "workflow_dispatch_latency_seconds",
    "Time from a task becoming claimable to a worker receiving it",
    # The boss fight grades p99 ≤ 50ms, so the buckets need resolution *around*
    # 50ms; a histogram whose nearest edges are 10ms and 100ms cannot tell you
    # whether you passed.
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0),
)


# ---- pre-initialised label sets --------------------------------------------
#
# A labelled collector renders *nothing* until some label combination has been
# used: `prometheus_client` creates a child on the first `.labels(...)` call, and
# an untouched parent has no samples to emit. On a freshly started engine that
# means `workflow_replays_total` is simply absent from /metrics — so a dashboard
# panel shows "No data", an alert on `rate(...)` never fires, and you find out
# during the incident rather than before it.
#
# Where the label space is small and known, the fix is to name every value up
# front so each series exists at zero from the first scrape. Where it is not —
# `task_queue` is caller-supplied and unbounded — you cannot, and that asymmetry
# is itself the reason to think twice before putting a user-controlled string in
# a label: every distinct value is a new time series, forever.
for _sticky in ("hit", "miss"):
    REPLAYS_TOTAL.labels(sticky=_sticky)
    EVENTS_REPLAYED.labels(sticky=_sticky)
for _outcome in ("completed", "failed"):
    EXECUTIONS_COMPLETED_TOTAL.labels(outcome=_outcome)
