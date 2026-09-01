"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register themselves into the default registry at
import time, and `common_telemetry.metrics_routes()` renders that same registry
at `/metrics` — so declaring a metric here is all the wiring there is. That
replaces the Rust side's install-a-global-recorder step entirely: no `install()`
to call, no ordering constraint against telemetry init, and no window at startup
where a metric is silently dropped.

The series the SPEC grades, and what each one is *for* during the boss fight:

* `COMMANDS_TOTAL` — labelled `cmd`; its rate is ops/sec by command, which is
  how you tell "SETs collapsed" from "everything collapsed".
* `COMMAND_DURATION` — a histogram, so p99 falls out at query time. See the
  bucket note below; the default buckets cannot answer this SPEC's question.
* `MEMTABLE_BYTES` — climbs on writes, drops to ~0 on flush. A sawtooth means
  flushing works; a ramp means it does not.
* `SSTABLES` — labelled `level`. **The write-stall signal.** A youngest-level
  count that keeps climbing while writes flow is compaction losing, and it
  climbs long before throughput visibly falls, which is what makes it worth
  watching rather than inferring.
* `COMPACTIONS_TOTAL` / `COMPACTION_BYTES_TOTAL` — compaction progress and the
  write-amplification meter (bytes rewritten ÷ bytes written by clients).
* `BLOCK_CACHE_LOOKUPS_TOTAL` — labelled `outcome`; hit ratio is
  `hit / (hit + miss)` at query time.
* `WAL_FSYNC_DURATION` — what the durability policy actually costs, in seconds.
* `CONNECTED_CLIENTS` — open RESP connections.

Two conventions worth copying rather than re-deriving:

**Counters per outcome, never a pre-computed ratio.** There is no
`block_cache_hit_ratio` gauge here on purpose. A ratio cannot be aggregated
across replicas or re-windowed after the fact; two counters can, and Prometheus
divides at query time. The same argument is why `COMMANDS_TOTAL` is labelled by
command rather than split into one metric per command.

**Buckets are a decision.** `prometheus_client`'s default histogram buckets
top out at 10 seconds with nothing between 1 ms and 10 ms — and the SPEC's
target is "p99 ≤ 10 ms", so the defaults would let you claim it while sitting
anywhere in a 9 ms-wide bucket. The buckets below are chosen to be dense exactly
where the target is. A histogram whose buckets do not bracket your SLO is a
metric that cannot fail.

Wiring the *call sites* — bumping these from `server.py`, `engine.py`, `wal.py`
and `compaction.py` — is the observability horizontal item, and is left for you.
This module just declares them and single-sources the names.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "BLOCK_CACHE_LOOKUPS_TOTAL",
    "COMMANDS_TOTAL",
    "COMMAND_DURATION",
    "COMPACTIONS_TOTAL",
    "COMPACTION_BYTES_TOTAL",
    "CONNECTED_CLIENTS",
    "MEMTABLE_BYTES",
    "SSTABLES",
    "WAL_FSYNC_DURATION",
]

COMMAND_BUCKETS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.0075,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    1.0,
)
"""Seconds. Dense from 100 µs (an in-memory hit) to 10 ms (the SPEC's p99
target), then coarse — past 100 ms you only need to know *that* it happened."""

FSYNC_BUCKETS = (0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0)
"""Seconds. A page-cache-only "fsync" lands under 100 µs and a real one on an
SSD lands around 1 ms — which is exactly how this histogram tells you whether
`WAL_SYNC=always` is doing what you think it is."""

# ---- Counters (rates) ------------------------------------------------------

COMMANDS_TOTAL = Counter(
    "lsm_commands_total",
    "Commands served, labelled by command name; the rate of this is ops/sec",
    ["cmd"],
)

COMPACTIONS_TOTAL = Counter(
    "lsm_compactions_total",
    "Compactions completed (V6)",
)

COMPACTION_BYTES_TOTAL = Counter(
    "lsm_compaction_bytes_total",
    "Bytes rewritten by compaction; against bytes written by clients this is write amplification",
)

BLOCK_CACHE_LOOKUPS_TOTAL = Counter(
    "lsm_block_cache_lookups_total",
    "Block-cache lookups; hit ratio = hit / (hit + miss)",
    ["outcome"],
)

# ---- Histograms (distributions) --------------------------------------------

COMMAND_DURATION = Histogram(
    "lsm_command_duration_seconds",
    "End-to-end command latency",
    ["cmd"],
    buckets=COMMAND_BUCKETS,
)

WAL_FSYNC_DURATION = Histogram(
    "lsm_wal_fsync_duration_seconds",
    "WAL fsync latency — what the durability policy costs (V2)",
    buckets=FSYNC_BUCKETS,
)

# ---- Gauges (current state) ------------------------------------------------

MEMTABLE_BYTES = Gauge(
    "lsm_memtable_bytes",
    "Approximate bytes held by the active memtable",
)

SSTABLES = Gauge(
    "lsm_sstables",
    "Live SSTables by level; a youngest-level count that keeps climbing is the write stall",
    ["level"],
)

CONNECTED_CLIENTS = Gauge(
    "lsm_connected_clients",
    "Currently-open RESP connections",
)
