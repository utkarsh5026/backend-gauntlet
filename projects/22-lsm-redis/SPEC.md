<!-- status:
state: not-started      # active | paused | blocked | done | not-started
blocked-on: ~           # free text, or ~ for none
-->

# Project 22 — LSM Storage Engine + Redis-Compatible Server

> This is the **keystone**. Every earlier project reached for a store — the message
> broker (#08) an append-only log, the Raft KV (#09) a state machine, the distributed
> cache (#07) a map behind a protocol real clients speak. Here you build the store
> itself, from the WAL up, and put a **RESP** front-end on it so `redis-cli` and
> `redis-benchmark` connect with no adapter. An LSM engine is what powers RocksDB,
> LevelDB, Cassandra, and the storage layer under half the databases you've used. It is
> "just a key/value store" the way a jet engine is "just a fan": the hard part is that
> it must be **durable** (survive `kill -9` with zero acknowledged writes lost), **fast
> to write** (turn random writes into sequential disk I/O), **fast to read** (bound the
> amplification that shape imposes), and **stay that way under sustained load** without
> the compaction debt piling up until writes stall. That last failure is the boss.

## What it does (the easy part)
- Speaks **RESP** on `:6379`, so `redis-cli` connects with no arguments.
- `SET key value`, `GET key`, `DEL key …`, `EXISTS`, `PING`, `AUTH` — a real subset of
  the redis command set, binary-safe keys and values.
- **Persists**: data survives a restart (WAL replay), and a crash mid-write never
  resurrects an unacknowledged write nor corrupts the store.
- `AUTH` gating when `REQUIREPASS` is set; a `/healthz`, `/stats`, `/config`, `/metrics`
  HTTP sidecar on a separate port.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips to
> ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. RESP protocol codec — *so real `redis-cli` connects*
Redis clients speak RESP over raw TCP: typed values back-to-back, a command is an array
of bulk strings. Build the codec in `src/lsm_redis/resp.py`: pull one complete command
off a byte buffer that may hold a partial frame *or* several pipelined ones, and
serialize replies.

**Done when ALL true:**
- [ ] `redis-cli -p 6379 ping` returns `PONG`, and `SET`/`GET`/`DEL` round-trip a value through a **stock `redis-cli`** — no custom client.
- [ ] **Partial frames** are handled: bytes delivered one at a time yield "need more" (no command) until the frame completes, and the read buffer is never advanced past an incomplete frame.
- [ ] **Pipelining** works: several commands arriving in one read are all executed, replies returned in order.
- [ ] **Binary-safe**: a key or value containing `\r`, `\n`, or NUL round-trips byte-for-byte.
- [ ] A declared bulk length over the configured cap is rejected as a **protocol error**, not an allocation.

**Proof:** an integration test driving a real `redis-cli`/redis client through
`SET`/`GET`/`DEL`; a `parse(encode(v)) == v` Hypothesis property (`test_resp_roundtrip`)
plus a byte-at-a-time partial-frame test.

*Concept to internalize:* framing a request/response protocol over a raw stream —
length-prefix vs delimiter, why "read a line" isn't enough, how pipelining falls out of
a buffer-oriented codec. **Stretch:** accept inline commands (`PING\r\n` typed by hand).

### V2. Write-ahead log — *durability before the acknowledgement*
A memtable lives in RAM; a crash before it reaches disk loses the write. Build the WAL in
`src/lsm_redis/wal.py`: append each mutation (framed, with a CRC) before it touches the
memtable, `fsync` per policy, and **replay** it on startup.

**Done when ALL true:**
- [ ] A `SET` is only acknowledged **after** its record is on the WAL per the sync policy — kill the process right after an `OK` and the value is present on restart.
- [ ] **Torn tail:** a crash mid-append leaves a partial final record; replay recovers everything *before* it and drops the partial — no exception, no resurrected write.
- [ ] **Corruption is caught:** a bit-flip in a committed record is detected by CRC on replay (reported, not silently served as data).
- [ ] The **sync policy** is configurable (`always` / `everysec` / `no`) and its meaning is honored — `always` fsyncs before ack.
- [ ] Replaying the WAL reconstructs the exact memtable state (same keys, values, tombstones, order).

**Proof:** a crash-recovery test (`kill -9` between ack and flush → no loss); a torn-tail
test (truncate the log mid-record → N-1 recovered, no error); a CRC test (flip a byte →
`Corrupt`); `docs/22-design.md` records the sync-policy default and the group-commit choice.

*Concept to internalize:* what durability means ("on stable storage before the ack"),
what `fsync` actually guarantees, and how a CRC turns a silent torn tail into a clean
truncation point. **Stretch:** group commit — one fsync amortized over many queued writes
(in Python, a single writer task draining an `asyncio.Queue`, waking a future per writer).

### V3. Memtable — *the sorted in-memory write buffer*
Every write lands here after the WAL. Build the buffer in `src/lsm_redis/memtable.py`: an
ordered map with sequence numbers and tombstones, plus size accounting that triggers a
freeze.

**Done when ALL true:**
- [ ] Entries are held **sorted by key** so a flush produces a sorted file with no re-sort.
- [ ] **Last-write-wins:** two writes to one key resolve to the higher-sequence value.
- [ ] A **delete inserts a tombstone**, not a removal — a read sees "deleted here" and does not fall through to an older value on disk.
- [ ] **Size is tracked** and crossing `MEMTABLE_MAX_BYTES` freezes the memtable (a fresh one takes writes) instead of blocking — writes keep flowing during the flush.
- [ ] Overwriting a key does **not** double-count its bytes in the size accounting.

**Proof:** Hypothesis properties for ordering (`test_memtable_sorted`), last-write-wins,
and tombstone semantics; a test that a full memtable freezes and a new one accepts writes
immediately.

*Concept to internalize:* why LSM trades read simplicity for write throughput (sequential
sorted flushes vs a B-tree's in-place random writes), and why a delete in a
log-structured store is an append, never an in-place erase. **Stretch:** the ordered
structure is a real choice in Python — `dict` + `sorted()` at flush time, `bisect.insort`
over a parallel key list, or `sortedcontainers.SortedDict`. Benchmark two and say in
`docs/22-design.md` which won and at what buffer size.

### V4. SSTable — *the immutable, sorted, on-disk file*
A frozen memtable flushes to a Sorted String Table in `src/lsm_redis/sstable.py`: sorted
KV pairs in blocks, a binary-searchable block index, a bloom filter (V5), and a footer.
Never modified after write.

**Done when ALL true:**
- [ ] A frozen memtable flushes to a single immutable file; a **restart re-opens it** and reads return the flushed values.
- [ ] A point lookup touches **~one data block**, located via the in-memory index (not a full-file scan) — verifiable by counting block reads.
- [ ] **Tombstones survive** the flush: a deleted key reads back as a tombstone from the file, not as present.
- [ ] A **crash mid-flush** cannot leave a half-written file that reads as valid (the footer/fsync ordering makes a partial flush recoverable — the WAL still holds those writes).
- [ ] A corrupt data block is **detected** on read (CRC), never returned as a wrong value.

**Proof:** a create→open→get round-trip test (values + `None` for absent + tombstones); a
block-read counter proving one-block lookups; a corruption test (flip a byte → `Corrupt`).

*Concept to internalize:* why immutability + sorted order + a sparse block index buy
O(log n) point lookups and cheap range scans on disk, and how "flush a sorted run, never
edit it" turns random writes into sequential file writes. **Stretch:** prefix-compress
keys within a block.

### V5. Bloom filters — *skip the files that can't hold the key*
A read miss would otherwise touch every SSTable. Build a bloom filter per SSTable in
`src/lsm_redis/bloom.py` so most misses are answered from memory.

**Done when ALL true:**
- [ ] **No false negatives:** every key written into an SSTable is reported *maybe-present* by its filter — a filter never hides a key that's really there.
- [ ] The guarantee **survives a restart**: a filter built in one process and read back in another reports every key it was given. (Python-specific — the built-in `hash()` is seeded per process and will silently break this.)
- [ ] A `GET` for a key **absent** from an SSTable is answered without reading any of its data blocks when the filter rejects it (verifiable via the block-read/cache-miss counter).
- [ ] The **false-positive rate** at `BLOOM_BITS_PER_KEY` is near the theoretical value (e.g. ~1% at 10 bits/key), measured over many absent keys.
- [ ] The filter is **persisted in the SSTable** and reloaded on open — not rebuilt by scanning the file.

**Proof:** a no-false-negatives Hypothesis property (`test_bloom_no_false_negatives`) plus
a cross-process variant (build, write, re-open in a subprocess, re-query); a measured
FP-rate test; a test that a filter-rejected key reads zero data blocks.

*Concept to internalize:* trading a little space and a tunable false-positive rate for
skipping disk I/O, and why the no-false-negatives guarantee is non-negotiable in a
database (a false negative silently drops a key that's on disk). **Stretch:** compare a
plain bloom vs a ribbon/blocked filter and note the difference.

### V6. Compaction — *keep the write debt from stalling the engine*
Every flush adds an SSTable; left alone, reads slow, deleted space is never reclaimed,
and eventually flushes outrun the disk and writes stall. Build background compaction in
`src/lsm_redis/compaction.py` (+ `Engine.run_compaction`): merge sorted runs into
fewer/larger ones, dropping shadowed values and reclaimable tombstones.

**Done when ALL true:**
- [ ] Merging SSTables that all touch a key leaves the **newest value** and removes the older copies from disk (a read still returns the newest value).
- [ ] A **tombstone is reclaimed** once nothing older beneath it survives — the deleted key stops appearing on disk and space is freed — but is **not** dropped while an older SSTable outside the merge still holds the key.
- [ ] Under a **sustained write load** with compaction running, the youngest-level SSTable count stays within a small factor of `L0_COMPACTION_TRIGGER` — it does not grow without bound.
- [ ] Compaction runs in the **background** without blocking foreground reads/writes (throughput doesn't collapse while it runs). On CPython this is the hardest criterion in the project — the merge is GIL-holding Python work, so where it runs is a decision, not a detail.
- [ ] The **policy** (size-tiered vs leveled) is chosen and its amplification tradeoff is stated.

**Proof:** a correctness test (post-merge read returns newest; old copies gone); a
tombstone-reclamation test (disk usage falls, key vanishes); a sustained-load test that
the L0 count stays bounded; a p99 measurement *during* a compaction showing foreground
latency did not collapse; `docs/22-design.md` names the policy, the amplification
tradeoff it favors, and where the merge executes (loop with cooperative yields, thread,
or process).

*Concept to internalize:* the write/read/space **amplification triangle** — you cannot
minimize all three; the compaction policy is exactly where you choose which to favor.
**Stretch:** leveled compaction with non-overlapping key ranges per level.

### V7. Block cache — *a hand-built LRU over decoded SSTable blocks*
Under a skewed workload the same blocks are read repeatedly. Build the cache in
`src/lsm_redis/block_cache.py` — no `functools.lru_cache` — so hot reads don't re-hit
disk.

**Done when ALL true:**
- [ ] A repeated read of a hot key is served **from the cache** (no disk block read on the second hit — verifiable via the hit/miss counter).
- [ ] The cache is bounded by **bytes** (`BLOCK_CACHE_BYTES`) and never exceeds it, regardless of block sizes or insert order.
- [ ] Eviction is **LRU**: under capacity pressure the least-recently-used block is evicted, not a hot one.
- [ ] The cache is **safe under concurrent readers** (many connections hit it at once) and each op stays O(1) — and whether that needs a lock is answered from where block reads actually execute, not assumed.
- [ ] `BLOCK_CACHE_BYTES=0` **disables** it cleanly (every read goes to disk, nothing grows).

**Proof:** an LRU-order test (touch A, insert past cap → A survives, LRU evicted); a
byte-bound test (`used_bytes ≤ cap` always); a hit-ratio test over a Zipfian read
sequence; `docs/22-design.md` notes the locking/sharding choice and what it is protecting
against.

*Concept to internalize:* how a block cache bounds an LSM's read amplification, why LRU
approximates "keep the working set," and the classic O(1) LRU structure. **Stretch:**
shard the cache to cut lock contention, or try a CLOCK/2Q policy and compare hit ratios.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] **RESP correctness:** replies use the right types (`+OK`, `:N`, `$…` bulk, `$-1` nil, `-ERR`) so a stock client renders them correctly (a `GET` miss shows `(nil)`, `DEL` shows an integer). *(Proof: redis-cli session / client test.)*
- [ ] **Unknown / wrong-arity commands** return a RESP error and keep the connection open (they don't drop or hang it). *(Proof: protocol test.)*
- [ ] **Graceful shutdown** stops accepting connections, finishes in-flight commands, and flushes the WAL before exit (SIGTERM loses nothing acknowledged). *(Proof: shutdown test.)*

### Caching
- [ ] Block cache implemented (V7) and consulted before disk on the read path.
- [ ] `docs/22-design.md` states how the OS **page cache** interacts with the block cache (double-caching, and whether you read with `os.pread` or `mmap` and why) — a named decision, not an accident. *(Proof: design doc.)*

### Security
- [ ] **`AUTH` enforced** when `REQUIREPASS` is set: any command before a successful `AUTH` is rejected `NOAUTH`, a wrong password is `WRONGPASS`, and the password never appears in logs, `/stats` or `/config`. *(Proof: auth reject test + a log scan.)*
- [ ] **Request bounds:** a single bulk over `MAX_REQUEST_BYTES` is rejected before allocation; a connection cap (redis `maxclients`) prevents a connection flood from exhausting fds/memory. *(Proof: oversized-request test + a connection-cap test.)*
- [ ] **Key/value size limits** are documented so one client can't stream the server out of disk. *(Proof: design doc + a limit test.)*

### Observability
- [ ] `structlog` structured logs; a slow-command log line carries command + latency (keys/values never logged verbatim).
- [ ] `/metrics` exports: **ops/sec by command, a command-latency histogram (p99), memtable bytes, SSTable count per level, compactions + bytes compacted, block-cache hit ratio, WAL fsync latency, connected clients.** *(Proof: a metrics-render test asserting the series exist after driving load.)*
- [ ] `/stats` reflects reality: memtable size rises on writes and drops on flush; SSTable count rises on flush and falls on compaction. *(Proof: a stats-transition test.)*

### Python & the runtime
- [ ] **pyright strict passes clean** — every `# type: ignore` carries a justifying comment.
- [ ] **No blocking call on the event loop** — runs clean under `PYTHONASYNCIODEBUG=1`; every `os.fsync`, `os.pread` and block decode is somewhere deliberate (thread pool, writer task, or process pool), and the design doc says which and why.
- [ ] **Bounded pool sized on purpose** — the read/fsync pool size and the connection cap are tuned *together*, with the reasoning in the design doc. An unbounded pool in front of a disk is a queue with extra steps.
- [ ] **Graceful shutdown** drains in-flight commands on SIGTERM via the FastAPI lifespan, and the RESP listener stops accepting before the WAL's final fsync.
- [ ] **Profile committed** — a `py-spy` flamegraph and a `memray` run in `docs/22-benchmarks.md`, naming the top bottleneck under the boss fight's load.

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load + crash tests live in `bench/`, the
   numbers in `docs/22-benchmarks.md`.
3. `docs/22-benchmarks.md` carries a **profile**, not just numbers: a `py-spy` flamegraph
   and a `memray` run taken under load, with the top bottleneck named. Numbers alone
   don't close this — you have to know *why* they are what they are, and on CPython that
   is where the interesting half of this project lives.
4. `docs/22-design.md` records the decisions the SPEC grades: **WAL sync policy +
   group-commit, memtable structure choice, freeze/flush handoff, SSTable block layout,
   `pread` vs `mmap` + the page-cache interaction, compaction policy (+ amplification
   tradeoff) and where the merge executes, block-cache policy + locking.**
5. `make verify` is green — `ruff format --check`, `ruff check`, `pyright` (strict) and
   `pytest` — and no `raise NotImplementedError` remains on a checked path.

## 🐉 Boss fight — The Write Stall

> A backfill job opens a firehose: millions of `SET`s, as fast as the socket allows,
> for a full minute — while a second client keeps reading hot keys and expecting
> answers. Every flush drops another SSTable onto L0. If compaction can't keep up, L0
> piles up, reads amplify across every file, memory balloons, and the engine does the
> one thing a database must avoid under load: it **stops accepting writes**. Real
> engines literally call this a *write stall*. Beat it by keeping the debt bounded — and
> prove that when you yank the power mid-flood, not one acknowledged write is lost.

**Arena:** `bench/` against the real entrypoint (`uv run lsm-redis`, so uvloop is the
loop — not the stdlib loop pytest gives you), driven by `redis-benchmark` (from the
reference redis container — `make bench`) plus a crash test that `kill -9`s the server
mid-flood and restarts it. Report hardware, and report the Python version and whether
uvloop was actually active.

**The boss falls when ALL true:**
- [ ] ≥ **20,000 SET ops/sec** sustained for 60s, and throughput **does not collapse** — the last-10s rate stays within 2× of the first-10s rate (no stall).
- [ ] ≥ **50,000 GET ops/sec** on a warm, Zipfian working set during that run.
- [ ] **p99 ≤ 10 ms** for both `GET` and `SET` across the run.
- [ ] Youngest-level **SSTable count stays ≤ 2× `L0_COMPACTION_TRIGGER`** throughout (compaction keeps up — read from `/metrics`, not vibes).
- [ ] **Block-cache hit ratio ≥ 95%** on the Zipfian read workload.
- [ ] **Memory stays bounded** — RSS holds within the configured caches + slack, it does not grow unbounded with the write count.
- [ ] **Zero acknowledged-write loss** across a `kill -9` mid-flood: every key the client saw `OK` for is present after restart (WAL replay), and the store is not corrupt.

> **These numbers are not scaled down for Python, deliberately.** They are what the Rust
> build was asked for, and they are what a real engine delivers. Where CPython cannot
> reach one, **the gap is the finding** — record in `docs/22-benchmarks.md` where it
> topped out and *why*, with the profile to back it: GIL contention during compaction,
> per-command bytecode in the codec, GC pauses under a large memtable, allocation churn
> in the parser, an `os.fsync` on the event loop. "Python is slower" is not a finding.
> "The parser allocates three `bytes` objects per command, which is 38% of the
> flamegraph at 50k ops/sec" is. Two legitimate escape hatches worth measuring rather
> than assuming: `SO_REUSEPORT` across several processes (each with its own shard of the
> keyspace — note what that costs you in consistency), and moving compaction into a
> `ProcessPoolExecutor`.

**Proof:** methodology + before/after numbers in `docs/22-benchmarks.md` (the stall you
saw before bounding compaction, and the bounded run after), hardware and interpreter
noted, commands reproducible via `bench/`.

## Suggested order of attack
1. **V1 first** — get `redis-cli ping` → `PONG` and `SET`/`GET` round-tripping straight
   into an in-memory `dict` (no persistence yet). Now you have a real client to test with.
2. **V2 WAL** — make writes durable; prove crash recovery and torn-tail handling.
3. **V3 memtable** — swap the dict for the sorted buffer with tombstones + size accounting.
4. **V4 SSTable** — flush a frozen memtable to disk; make reads reconcile memtable + files.
5. **V5 bloom** — add per-SSTable filters; watch read misses stop touching disk.
6. **V7 block cache** — cache hot blocks; measure the hit ratio climb.
7. **V6 compaction** — turn on the background compactor; bound L0 under sustained writes.
8. Add `AUTH` + request bounds (security), wire the metrics call sites, then **benchmark,
   document, tune** until the Write Stall falls.

## Run it
```bash
make setup                           # .env from .env.example
make sync                            # uv sync (the root uv.lock is authoritative)
make up                              # a reference redis (host port 6322) to compare against
make run                             # your server: RESP on :6379, HTTP on :8080

# talk to YOUR server with a stock client:
make ping                            # redis-cli ping / set / get / del
redis-cli -p 6379 ping
redis-cli -p 6379 set hello world
redis-cli -p 6379 get hello

# see the engine's real state — the files ARE the database:
make data                            # WAL bytes + SSTable count on disk
make stats                           # GET /stats
make metrics                         # GET /metrics

# prove durability (V2): write a key, then
make crash                           # kill -9 — no drain, no fsync
make run                             # …and ask for the key back

# compare against the reference redis, or generate load for the boss:
redis-cli -p 6322 ping                                   # the reference server
make bench                                               # redis-benchmark -t set,get -n 100000 -P 16
PID=$(pgrep -f lsm-redis) make profile                   # py-spy flamegraph under that load

make verify                          # fmt-check → lint → types → test (what CI runs)
```
