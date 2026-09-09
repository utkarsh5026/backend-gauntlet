#!/usr/bin/env python3
"""FileCas vs Haystack for tiny objects — the small-object packing tradeoff.

Drives `Store.commit_bytes` / `Store.open_blob` directly, with no HTTP, so the
delta measured is physical layout — inodes, appends, ranged reads — and not the
router. See this directory's README for the honest-measurement checklist.

    make bench-haystack
    COUNT=20000 SIZE=4K uv run python bench/haystack_small/main.py
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from object_store.config import BlobLayoutKind  # noqa: E402
from object_store.objects import Digest  # noqa: E402
from object_store.store import Store  # noqa: E402

RESULTS = Path(__file__).resolve().parents[1] / "results"


def parse_size(raw: str) -> int:
    """`4K`, `1M`, `512` → bytes."""
    text = raw.strip().upper()
    for suffix, scale in (("K", 1024), ("M", 1024**2), ("G", 1024**3)):
        if text.endswith(suffix):
            return int(float(text[:-1]) * scale)
    return int(text)


@dataclass(slots=True)
class Phase:
    """Latency samples for one timed phase."""

    name: str
    samples: list[float] = field(default_factory=list)

    def record(self, seconds: float) -> None:
        self.samples.append(seconds)

    @property
    def ops_per_second(self) -> float:
        total = sum(self.samples)
        return len(self.samples) / total if total else 0.0

    def percentile(self, fraction: float) -> float:
        """Latency in milliseconds at `fraction`.

        Nearest-rank rather than an interpolating quantile: for a p99 over a few
                thousand samples an interpolated value can sit between two real
                measurements, which is fine for describing a distribution and misleading
                for a tail you are trying to explain.
        """
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = min(int(fraction * len(ordered)), len(ordered) - 1)
        return ordered[index] * 1000


def payload(index: int, size: int) -> bytes:
    """A unique payload per index.

    Stamping each one matters: identical bytes dedup, and a benchmark whose
    second half is all dedup hits measures the short-circuit, not the layout.
    """
    stamp = f"needle-{index:09d}-".encode()
    return stamp + bytes((index + position) % 256 for position in range(size - len(stamp)))


async def run_layout(
    layout: BlobLayoutKind, count: int, size: int, warmup: int, volume_cap: int
) -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix=f"haystack-{layout.value}-"))
    try:
        store = Store(root, layout=layout, max_volume_size=volume_cap)

        for index in range(warmup):
            await store.commit_bytes(payload(-index - 1, size))

        writes = Phase("write")
        digests: list[Digest] = []
        for index in range(count):
            data = payload(index, size)
            started = time.perf_counter()
            digests.append(await store.commit_bytes(data))
            writes.record(time.perf_counter() - started)

        object_files = sum(1 for p in (root / "objects").rglob("*") if p.is_file())
        volume_files = sum(1 for p in (root / "volumes").glob("*.dat"))
        on_disk = sum(
            p.stat().st_size
            for name in ("objects", "volumes")
            for p in (root / name).rglob("*")
            if p.is_file()
        )

        # Shuffled so the read phase cannot ride sequential locality that a real
        # workload would not have.
        reads = Phase("read")
        random.Random(7).shuffle(digests)
        for digest in digests:
            started = time.perf_counter()
            reader = await store.open_blob(digest)
            try:
                while reader.read(64 * 1024):
                    pass
            finally:
                reader.close()
            reads.record(time.perf_counter() - started)

        store.close()
        return {
            "layout": layout.value,
            "write_ops_per_sec": round(writes.ops_per_second, 1),
            "write_p50_ms": round(writes.percentile(0.50), 3),
            "write_p99_ms": round(writes.percentile(0.99), 3),
            "read_ops_per_sec": round(reads.ops_per_second, 1),
            "read_p50_ms": round(reads.percentile(0.50), 3),
            "read_p99_ms": round(reads.percentile(0.99), 3),
            "object_files": object_files,
            "volume_files": volume_files,
            "bytes_on_disk": on_disk,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def main() -> None:
    count = int(os.environ.get("COUNT", "5000"))
    size = parse_size(os.environ.get("SIZE", "4K"))
    warmup = int(os.environ.get("WARMUP", "100"))
    # 1 GiB by default here, unlike the store's 1 MiB: thousands of 4 KiB
    # needles must land in *one* volume or the comparison measures volume
    # rollover instead of packing.
    volume_cap = parse_size(os.environ.get("HAYSTACK_MAX_VOLUME_SIZE", "1G"))

    print(f"count={count} size={size}B warmup={warmup} volume_cap={volume_cap}B\n")

    rows = [
        await run_layout(layout, count, size, warmup, volume_cap)
        for layout in (BlobLayoutKind.FILE_CAS, BlobLayoutKind.HAYSTACK)
    ]

    header = (
        f"{'layout':<10} {'w_ops/s':>10} {'w_p50':>8} {'w_p99':>8} "
        f"{'r_ops/s':>10} {'r_p50':>8} {'r_p99':>8} {'obj_n':>8} {'vol_n':>7}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['layout']:<10} {row['write_ops_per_sec']:>10} "
            f"{row['write_p50_ms']:>8} {row['write_p99_ms']:>8} "
            f"{row['read_ops_per_sec']:>10} {row['read_p50_ms']:>8} "
            f"{row['read_p99_ms']:>8} {row['object_files']:>8} {row['volume_files']:>7}"
        )

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"haystack_small-{int(time.time())}.json"
    out.write_text(
        json.dumps(
            {"count": count, "size": size, "volume_cap": volume_cap, "rows": rows},
            indent=2,
        )
    )
    print(f"\n→ {out}")
    print("Curate the table you care about into docs/06-benchmarks.md.")


if __name__ == "__main__":
    asyncio.run(main())
