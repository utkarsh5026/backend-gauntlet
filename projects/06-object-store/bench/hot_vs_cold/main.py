#!/usr/bin/env python3
"""Hot vs cold tier GET cost — what transparent tiering actually charges.

Same keys, same plaintext, two encodings. A client cannot tell which tier served
it, which is the point; this measures what that transparency costs in latency
and what it buys in bytes on disk.

In-process rather than over HTTP, because tiering is driven by
`Lifecycle.run_once_at(simulated_now)` — a daemon seam, not an HTTP verb. That
means no waiting real days for `tier_after_days`, no race with a background
sweeper, and the tier can be *asserted* via `locate` before the cold numbers are
taken. Network overhead is deliberately excluded so the delta is decode cost and
cold I/O shape, not TCP.

    make bench-tier
    SIZES=1M,8M ITERS=20 uv run python bench/hot_vs_cold/main.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from object_store.index import Index, NewVersion  # noqa: E402
from object_store.index_backend import LocalIndex  # noqa: E402
from object_store.lifecycle import (  # noqa: E402
    Encoding,
    Lifecycle,
    LifecyclePolicy,
    LifecycleRule,
)
from object_store.naming import Bucket, Key  # noqa: E402
from object_store.objects import ETag, utc_now  # noqa: E402
from object_store.store import Store  # noqa: E402

RESULTS = Path(__file__).resolve().parents[1] / "results"
BUCKET = Bucket("bench")


def parse_size(raw: str) -> int:
    text = raw.strip().upper()
    for suffix, scale in (("K", 1024), ("M", 1024**2), ("G", 1024**3)):
        if text.endswith(suffix):
            return int(float(text[:-1]) * scale)
    return int(text)


def compressible(size: int) -> bytes:
    """Repeating text — the best case for the cold tier."""
    unit = b"the quick brown fox jumps over the lazy dog. "
    return (unit * (size // len(unit) + 1))[:size]


def incompressible(size: int) -> bytes:
    """Pseudo-random bytes — almost no size win, and you still pay to decode."""
    return random.Random(1234).randbytes(size)


async def drain(reader: object) -> int:
    total = 0
    try:
        while chunk := reader.read(256 * 1024):  # type: ignore[attr-defined]
            total += len(chunk)
    finally:
        reader.close()  # type: ignore[attr-defined]
    return total


async def measure(open_reader, iterations: int, warmup: int) -> tuple[float, float]:  # noqa: ANN001
    """`(mean_ms, throughput_MiB_per_s)` over `iterations` full reads."""
    for _ in range(warmup):
        await drain(await open_reader())

    samples: list[float] = []
    total_bytes = 0
    for _ in range(iterations):
        started = time.perf_counter()
        total_bytes += await drain(await open_reader())
        samples.append(time.perf_counter() - started)

    elapsed = sum(samples)
    mean_ms = (elapsed / len(samples)) * 1000
    throughput = (total_bytes / elapsed) / (1024**2) if elapsed else 0.0
    return mean_ms, throughput


async def run_case(name: str, make_payload, size: int, iterations: int, warmup: int):  # noqa: ANN001, ANN201
    root = Path(tempfile.mkdtemp(prefix="hot-vs-cold-"))
    try:
        store = Store(root)
        index = Index(root, store, gc_grace=0.0)
        backend = LocalIndex(index)
        lifecycle = Lifecycle(backend, store)

        await index.create_bucket(BUCKET)
        payload = make_payload(size)
        digest = await store.commit_bytes(payload)
        key = Key(f"{name}-{size}")
        await index.put(
            BUCKET,
            key,
            NewVersion(
                digest=digest,
                etag=ETag(hashlib.md5(payload).hexdigest()),
                size=len(payload),
                content_type="application/octet-stream",
            ),
        )

        hot_bytes = store.blob_path(digest).stat().st_size
        hot_ms, hot_throughput = await measure(lambda: store.open_blob(digest), iterations, warmup)

        metadata = await index.load_bucket_metadata(BUCKET)
        metadata.lifecycle = LifecyclePolicy(rules=[LifecycleRule(tier_after_days=1)])
        await index.store_bucket_metadata(BUCKET, metadata)
        report = await lifecycle.run_once_at(utc_now() + timedelta(days=2))

        physical = await lifecycle.locate(digest)
        assert physical.encoding is Encoding.ZSTD, "blob did not reach the cold tier"
        cold_bytes = lifecycle.cold_path(digest).stat().st_size

        cold_ms, cold_throughput = await measure(
            lambda: lifecycle.open_tiered(digest), iterations, warmup
        )
        store.close()

        return {
            "case": name,
            "size": size,
            "hot_bytes": hot_bytes,
            "cold_bytes": cold_bytes,
            "ratio": round(cold_bytes / hot_bytes, 3) if hot_bytes else 0.0,
            "hot_mean_ms": round(hot_ms, 3),
            "cold_mean_ms": round(cold_ms, 3),
            "hot_mib_per_s": round(hot_throughput, 1),
            "cold_mib_per_s": round(cold_throughput, 1),
            "tiered": report.tiered,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def main() -> None:
    sizes = [parse_size(s) for s in os.environ.get("SIZES", "1M,4M,16M").split(",")]
    iterations = int(os.environ.get("ITERS", "10"))
    warmup = int(os.environ.get("WARMUP", "2"))

    print(f"sizes={sizes} iters={iterations} warmup={warmup}")
    print(
        "note: a second read of the same blob is often page cache, not disk. "
        "For headline numbers drop caches between phases.\n"
    )

    rows = []
    for size in sizes:
        for name, maker in (
            ("compressible", compressible),
            ("incompressible", incompressible),
        ):
            rows.append(await run_case(name, maker, size, iterations, warmup))

    header = (
        f"{'case':<15} {'size':>9} {'hot_MiB':>9} {'cold_MiB':>9} {'ratio':>7} "
        f"{'hot_ms':>9} {'cold_ms':>9} {'hot_MB/s':>10} {'cold_MB/s':>10}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['case']:<15} {row['size'] / 1024**2:>8.1f}M "
            f"{row['hot_bytes'] / 1024**2:>9.2f} {row['cold_bytes'] / 1024**2:>9.2f} "
            f"{row['ratio']:>7} {row['hot_mean_ms']:>9} {row['cold_mean_ms']:>9} "
            f"{row['hot_mib_per_s']:>10} {row['cold_mib_per_s']:>10}"
        )

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"hot_vs_cold-{int(time.time())}.json"
    out.write_text(json.dumps({"rows": rows}, indent=2))
    print(f"\n→ {out}")
    print("Curate into docs/06-benchmarks.md under 'Hot vs cold tier'.")


if __name__ == "__main__":
    asyncio.run(main())
