"""V1 — The ingest parser + the series fingerprint.

Turn a wire line into typed `MetricPoint`s and give every series a stable
identity. This is the front door of the pipeline: get the data model wrong here
and every downstream stage (rollup, sink, query) inherits the bug.

The wire format is a *line protocol* — the InfluxDB shape is a good target::

    measurement,tag1=v1,tag2=v2 field1=val1,field2=val2 timestamp
    cpu,host=a,region=us         usage=0.91,sys=0.12     1719600000

The two traps to internalize (see SPEC V1): **series identity** — the fingerprint
must be order-independent, so tags are sorted before hashing — and
**cardinality**, the product of distinct tag values, which is your cost function.

Scaffold state: both functions raise. The first `POST /ingest` blows up with the
NotImplementedError message — that is your worklist.
"""

from __future__ import annotations

from datetime import datetime

from .model import MetricPoint, Series, SeriesId

__all__ = ["fingerprint", "parse"]


def fingerprint(series: Series) -> SeriesId:
    """Compute the stable fingerprint of a series.

    Same measurement + same set of tags => same id, regardless of the order the
    tags were written on the wire. That order-independence is the whole point:
    `a=1,b=2` and `b=2,a=1` are the *same* series and must collide here.
    """
    # TODO(V1): hash the measurement and the tags into an int. Notes:
    #   - sort tags by key FIRST (or require the caller to) so the hash is
    #     canonical — this is the load-bearing step.
    #   - do NOT use the builtin `hash()`. CPython salts `hash(str)` with
    #     PYTHONHASHSEED, which is random per process — the same series would
    #     fingerprint differently after a restart, silently splitting every
    #     graph in two. This is the Python-specific version of the trap, and it
    #     is worth internalising: `hash()` is for in-process dict placement, not
    #     for identity that outlives the process.
    #   - reach for `hashlib.blake2b(digest_size=8)` (fast, stable, and you can
    #     take the first 8 bytes with `int.from_bytes`), or write FNV-1a in five
    #     lines if you want to see the arithmetic. Either way, feed it *bytes*
    #     with an explicit encoding.
    #   - include a separator between fields so `ab|c` and `a|bc` don't collide.
    raise NotImplementedError("V1: fingerprint the measurement + sorted tags into a SeriesId")


def parse(payload: str, *, now: datetime | None = None) -> list[MetricPoint]:
    """Parse a full line-protocol payload (possibly many lines) into points.

    One wire line with N fields expands to N points (one per field), each its own
    series. A malformed line is a hard error here — the caller rejects the
    request with a 400 and increments a `points_rejected` counter; a bad line
    must never silently corrupt the batch downstream.

    `now` is injectable so a test can pin the default timestamp instead of racing
    the clock. Defaults to `datetime.now(UTC)` when a line omits its timestamp.

    Raises:
        BadRequest: the payload contains a line this parser rejects.
    """
    # TODO(V1): parse `payload` into points. Suggested shape:
    #   - split on newlines; skip blank lines and `#` comments.
    #   - for each line, split into three space-separated sections:
    #       <measurement,tagset>  <fieldset>  [timestamp]
    #     `line.split(" ", 2)` gets you there without a regex; be deliberate
    #     about what an escaped space inside a tag value should do.
    #   - parse the tagset into (key, value) pairs, then `tuple(sorted(pairs))`
    #     -> build a `Series`. The sort is what makes `fingerprint` canonical.
    #   - parse the fieldset (`k=v,k=v`) into one MetricPoint per field; how a
    #     field name maps into the series is YOUR model choice (fold it into the
    #     measurement, or carry it as a reserved tag).
    #   - parse the optional trailing timestamp; default to `now`. Build it with
    #     `datetime.fromtimestamp(ts, tz=UTC)` — never the naive overload, and
    #     never `utcnow()` (deprecated in 3.12 precisely because it lies about
    #     the tzinfo). Reject an absurd timestamp (far past/future): that is a
    #     V1 validation lesson and a security one (bad ts -> bad partition).
    #   - VALIDATE + CAP: line length, tag count, key/value charset and length.
    #     An unbounded tag is the cardinality DoS vector (SPEC: security).
    #   - raise `BadRequest` (from `.errors`) on a malformed line — the handler
    #     already maps it to a 400.
    #
    # Performance note you will care about at the boss fight: this runs once per
    # ingested line and again in the consumer, so it is the single hottest pure-
    # Python function in the process. `str.split` on a `str` beats a regex here,
    # and decoding the body once (`bytes.decode`) beats decoding per line.
    raise NotImplementedError("V1: parse line protocol into MetricPoints (reject malformed lines)")
