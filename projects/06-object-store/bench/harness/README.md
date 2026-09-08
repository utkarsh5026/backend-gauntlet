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
SIZE_MB=256 FRACTIONS=0.1,0.5,0.9 uv run python bench/harness/crash.py
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

### Both outcomes have to occur, and the script enforces it

A crash test where every kill lands on the same side of the commit proves half
the claim and looks identical to a passing run. So the schedule sweeps kills
across the upload window *and* ends with one that fires only after the child
reports its PUT returned — at which point the object is provably committed, and
"whole" stops depending on luck. The script exits non-zero if either outcome is
missing.

Two bugs in this harness were worth more than the result it produces:

* **The baseline lied.** Timing a PUT in-process and then expressing kill times
  as fractions of it does not work, because the run being killed goes through a
  subprocess with its own startup and read costs. Every kill landed early, every
  attempt reported "nothing", and the harness looked green while testing one
  branch. It now times the baseline through the same child, from the same `GO`
  marker.
* **The signal went to the wrong process.** `start_store` originally launched
  `uv run object-store`, so `SIGTERM`/`SIGKILL` hit the wrapper while the real
  server kept running and kept the port. The next attempt then connected to a
  dying listener and failed with "connection reset by peer" — from a test whose
  entire job is distinguishing a crash from a clean shutdown. It now launches
  the console script next to `sys.executable`, and waits for the port to be
  genuinely free before moving on.

Both are the same lesson the Dockerfile's `ENTRYPOINT` comment makes: whatever
receives the signal has to be the thing you meant to signal.

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
