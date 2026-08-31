#!/usr/bin/env python3
"""Throughput benchmark for the Snowflake-style ID generator (V1).

Run with:  `make bench`  (or `uv run python bench/id_gen_bench.py`)

WHAT TO READ OFF THE NUMBERS
----------------------------
`next_id` is gated by the 12-bit sequence field: at most MAX_SEQUENCE (4096) ids
per node per millisecond. A tight loop blows past that budget and the generator
waits for the next wall-clock millisecond. So there is a hard ceiling:

    4096 ids/ms x 1000 ms/s = 4,096,000 ids/sec  PER NODE

That ceiling is a property of the *bit layout*, not of the language or the CPU,
so it is the one number that should look the same here as it did in Rust. If the
measured rate sits well below it, the bottleneck is interpreter overhead; if it
sits at it, the design is the limit and the answer is more nodes, not a faster
loop. Either way the finding belongs in `docs/01-benchmarks.md`.

Threads do **not** raise the per-node number: one node shares one 4096/ms budget,
and extra threads only add lock contention. The `--threads` run measures exactly
that, and a Python-specific effect on top - the GIL means the threads are not
running the arithmetic in parallel anyway.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from url_shortener.id_gen import (  # noqa: E402
    MAX_SEQUENCE,
    IdGenerator,
    assemble_id,
    base62_encode,
    decode,
)

CEILING_PER_NODE = MAX_SEQUENCE * 1_000


def _time(label: str, iterations: int, work: Callable[[int], object]) -> float:
    """Run `work(iterations)` and report operations per second."""
    started = time.perf_counter()
    work(iterations)
    elapsed = time.perf_counter() - started
    rate = iterations / elapsed
    print(f"  {label:<34} {rate:>14,.0f} ops/sec   ({elapsed:.3f}s for {iterations:,})")
    return rate


def bench_next_id(iterations: int) -> float:
    generator = IdGenerator(1)

    def work(count: int) -> None:
        for _ in range(count):
            generator.next_id()

    return _time("next_id (clock-gated)", iterations, work)


def bench_next_id_and_slug(iterations: int) -> float:
    generator = IdGenerator(1)

    def work(count: int) -> None:
        for _ in range(count):
            generator.next_id_and_slug()

    return _time("next_id_and_slug", iterations, work)


def bench_assemble(iterations: int) -> float:
    """Pure bit packing, with the clock and the lock taken out of the picture."""

    def work(count: int) -> None:
        for index in range(count):
            assemble_id(index, index % MAX_SEQUENCE, 1)

    return _time("assemble_id (pure arithmetic)", iterations, work)


def bench_base62(iterations: int) -> float:
    value = assemble_id(1_000_000_000, 4095, 1023)

    def work(count: int) -> None:
        for _ in range(count):
            base62_encode(value)

    return _time("base62_encode", iterations, work)


def bench_decode(iterations: int) -> float:
    value = assemble_id(1_000_000_000, 4095, 1023)

    def work(count: int) -> None:
        for _ in range(count):
            decode(value)

    return _time("decode", iterations, work)


def bench_threaded(iterations: int, threads: int) -> float:
    """The same total work, split across OS threads.

    Expect this to be *no faster*, and quite possibly slower. Two reasons stack
    up: the 4096/ms budget is per node and shared, and the GIL means the threads
    take turns through the interpreter regardless.
    """
    generator = IdGenerator(1)
    per_thread = iterations // threads

    def work(_count: int) -> None:
        def mint() -> None:
            for _ in range(per_thread):
                generator.next_id()

        workers = [threading.Thread(target=mint) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

    return _time(f"next_id across {threads} threads", per_thread * threads, work)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--iterations", type=int, default=200_000)
    parser.add_argument("-t", "--threads", type=int, default=4)
    parser.add_argument("-r", "--repeat", type=int, default=3)
    args = parser.parse_args()

    print(f"\nid_gen throughput — {args.iterations:,} iterations x {args.repeat} runs")
    print(f"theoretical ceiling: {CEILING_PER_NODE:,} ids/sec per node ({MAX_SEQUENCE} per ms)\n")

    next_id_rates: list[float] = []
    for run in range(1, args.repeat + 1):
        print(f"run {run}/{args.repeat}")
        next_id_rates.append(bench_next_id(args.iterations))
        bench_next_id_and_slug(args.iterations)
        bench_assemble(args.iterations)
        bench_base62(args.iterations)
        bench_decode(args.iterations)
        bench_threaded(args.iterations, args.threads)
        print()

    best = max(next_id_rates)
    print(
        f"best next_id: {best:,.0f} ids/sec "
        f"({best / CEILING_PER_NODE:.1%} of the {CEILING_PER_NODE:,}/sec ceiling)"
    )
    print(f"median next_id: {statistics.median(next_id_rates):,.0f} ids/sec\n")


if __name__ == "__main__":
    main()
