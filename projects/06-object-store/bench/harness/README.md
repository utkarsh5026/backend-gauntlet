# bench/harness — the Definition-of-done proof

The SPEC asks for four measurements, and this directory produces them:

| What | Where | Why it is the number that matters |
|------|-------|-----------------------------------|
| Sustained upload/download MB/s | `main.py` | The headline, and the least interesting on its own |
| **RSS stays flat** streaming an object ≫ RAM | `main.py` | V2's entire payoff; a fast disk cannot fake it |
| Dedup: N identical PUTs → 1 blob | `main.py` | V1's entire payoff, as a saved-bytes ratio |
| Multipart ETag matches S3's `-N` formula | `main.py` | V4's compatibility test, computed independently |
| `kill -9` mid-PUT never yields a truncated object | `crash.py` | V1's crash-consistency claim |

```bash
make bench                                    # the four in-process measurements
SIZE=1G PUTS=32 PART_SIZE=32M make bench      # bigger

uv run python bench/harness/crash.py          # the crash test (spawns real processes)
ITERATIONS=10 KILL_AFTER=0.3 uv run python bench/harness/crash.py
```

## The trap that makes a streaming server look broken

`main.py` measures the download against the **raw ASGI app**, not through
httpx. This is not fussiness: httpx's `ASGITransport` accumulates the entire
response body in a list before returning a response, so RSS measured around an
httpx download reports the *client's* buffer. On a 256 MiB object that is about
1 GiB of growth — which looks exactly like the bug V2 exists to prevent, in a
server that is streaming perfectly.

The same trap has a second edge: the harness generates its request bodies from
an async generator rather than a `bytes` object, because a harness that
materialises the payload to prove the server does not is measuring its own
memory.

## Why the crash test is a separate script

Everything else runs in-process. Crash consistency cannot: proving that a
`kill -9` during a PUT leaves either the whole object or nothing needs a real
process to kill, at a real moment, with a real page cache. `crash.py` starts the
store as a subprocess, begins a large upload, `SIGKILL`s it — not `SIGTERM`,
since a graceful shutdown is precisely what is *not* being tested — restarts
over the same data dir, and then re-hashes every blob under `objects/`.

The failure it is looking for is the third outcome: a `200` whose bytes do not
match the digest they are stored under. That is what the
temp → fsync → rename → fsync-dir sequence exists to make impossible, and it is
undetectable afterwards because the blob's *name* still looks correct.

## Reading the numbers honestly

1. **In-process means no TCP.** These numbers are routing + streaming + the
   index write. A real client over a real network will be slower, and the gap
   between the two is itself worth knowing.
2. **Page cache.** A GET immediately after the PUT that wrote it is often RAM.
   For disk-bound download numbers, drop caches between the phases.
3. **`ru_maxrss` is a high-water mark.** It only climbs, which is what makes it
   the right measurement here — a single buffered object would raise the peak
   and never lower it again.
4. **Say *why*.** Python's ceiling on the PUT path is not one thing: `hashlib`
   releases the GIL on large buffers so hashing genuinely parallelises, while
   per-chunk interpreter overhead does not. Which of the two is the wall depends
   on the chunk size, and finding out is the point — `make profile` renders the
   flamegraph that answers it.

Curated results belong in `docs/06-benchmarks.md`, with the reasoning next to
the figures.
