# Concept Bank — Project 22: LSM Storage Engine + Redis-Compatible Server

> This is the map of what the keystone should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted. This is the storage engine every earlier project reached for — now you know what it costs.

---

## 🧠 Card 1 — RESP: framing a protocol real clients speak *(V1 · `src/resp.rs`)*

**The problem.** TCP hands you a byte stream with no message boundaries: `SET hello world` may arrive one byte at a time, or glued to three more pipelined commands in a single read. "Read a line" fails twice — values are binary (a value containing `\r\n` shreds line-based parsing) and a lying length header is an allocation bomb. And you don't get to define the protocol: `redis-cli` already speaks RESP, and it is the compatibility test.

**The idea.** RESP frames typed values back-to-back — `+OK`, `:42`, `$5\r\nhello` (length-prefixed bulk, hence binary-safe), `*2…` arrays; a command is an array of bulk strings. The codec is buffer-oriented: try to decode one complete frame from the front of the buffer; if incomplete, return "need more" *without consuming anything*; if complete, consume exactly that frame. Pipelining then falls out for free — several complete frames in the buffer decode in sequence. Declared lengths are validated against a cap *before* allocation.

**In the wild:** RESP is one of the most-implemented wire protocols alive (every Redis client/proxy/clone); the framing discipline is the same as project 19's peer wire and every binary protocol since.

**You own it when you can explain:**
- [ ] Why length-prefixing (not delimiters) is what makes RESP binary-safe — the `\r\n`-inside-a-value counterexample.
- [ ] The partial-frame contract: what the buffer pointer does on "need more" and why consuming a half-frame corrupts everything after it.
- [ ] Why pipelining requires zero extra code in a correct buffer-oriented codec — and what latency pipelining actually eliminates (per-command round trips).
- [ ] The cap-before-allocate rule at the exact line it applies (`$4294967295\r\n`…).
- [ ] The reply-type mapping a stock client renders: `$-1` nil for a missed GET, `:N` for DEL, `-ERR` keeping the connection alive.

**Depth probes:**
- Why does `redis-benchmark` throughput collapse without pipelining? Do the RTT arithmetic at 50k ops/s.
- Inline commands (`PING\r\n` typed into netcat) — what does supporting both framings cost the parser?

**Trap:** testing the codec only with whole-message writes. The split-frame and glued-frame cases *are* the codec; loopback tests naturally deliver tidy packets and hide both.

---

## 🧠 Card 2 — The WAL: durability before the acknowledgement *(V2 · `src/wal.rs`)*

**The problem.** The fast write path is an in-memory table — and a crash vaporizes it. The moment your server says `OK`, the client is entitled to that write *forever*, across `kill -9`, power loss, and whatever the page cache was doing. But `write()` returning success means almost nothing: the bytes are in kernel memory, not on the platter. And a crash mid-append leaves a *torn tail* — half a record that a naive replay would deserialize into garbage state.

**The idea.** Write-ahead logging: append each mutation — length-framed, CRC-stamped — to a sequential log and `fsync` per policy *before* acknowledging; only then apply to the memtable. Recovery replays the log into an identical memtable, and the CRC turns the torn tail into a clean truncation point (everything before it recovered, the partial record dropped, nothing resurrected). The sync policy is the honest dial — `always` (fsync before every ack: zero loss, slower), `everysec` (bounded loss window), `no` (page-cache roulette) — and group commit amortizes one fsync across many queued writers.

**In the wild:** Postgres WAL, MySQL redo log, RocksDB WAL, Redis AOF (`appendfsync` is literally this dial), etcd — every durable store on earth has this exact component; project 08's log was this idea as the *product*.

**You own it when you can explain:**
- [ ] The full residence chain (user buffer → page cache → disk cache → platter) and which syscall moves which arrow — why `write()` ≠ durable.
- [ ] "Durability" as a precise claim: on stable storage *before the ack* — and why the ack, not the write, is the promise point.
- [ ] Torn-tail recovery step by step: how length+CRC framing identifies the last complete record, and why truncation (not error) is the right response.
- [ ] What CRC catches that fsync cannot (partial sector writes, bit rot) — they solve different failures.
- [ ] The sync-policy dial with its loss window per setting, and how group commit gets `always`-like safety at batch cost.
- [ ] Why WAL-then-memtable ordering is non-negotiable (the reverse acks state that recovery can't rebuild).

**Depth probes:**
- Why is appending to one sequential log fast enough to sit in front of *every* write? (Sequential I/O — the same physics as project 08.)
- fsync on the WAL file vs its directory (project 06's lesson): when does the *file's* durability need the directory's?

**Trap:** proving durability with tests that exit cleanly. Process exit flushes; `kill -9` between ack and fsync is the only honest test — and even that spares the page cache, which is why the policy reasoning matters beyond what tests can show.

---

## 🧠 Card 3 — Memtable + SSTable: the LSM write path *(V3+V4 · `src/memtable.rs`, `src/sstable.rs`)*

**The problem.** B-trees update pages *in place* — every write is a random I/O somewhere in a big file, and random writes are the slowest thing storage does. To absorb a write firehose you want every disk write to be sequential — but you still owe fast reads and real deletes, and "just append everything" makes reads a full scan.

**The idea.** The LSM bargain. Writes land in a **memtable** — a sorted in-memory map with sequence numbers (last-write-wins) — and deletes insert **tombstones** (in a log-structured world you can't erase history; you append "this key is dead here" so reads stop before older on-disk values). When full, the memtable *freezes* and a fresh one takes writes (no pause), while the frozen one flushes — already sorted — as an **SSTable**: an immutable file of sorted blocks + a sparse block index + a bloom filter + a footer. Point reads binary-search the index and touch ~one block. The read path reconciles newest-to-oldest: memtable → frozen memtables → SSTables — order *is* correctness.

**In the wild:** RocksDB/LevelDB (this exact architecture), Cassandra/ScyllaDB, InfluxDB, Lucene's segments (project 20 — same shape, search clothing); "LSM vs B-tree" is *the* storage-engine tradeoff interview question, and you'll have built one side.

**You own it when you can explain:**
- [ ] The LSM-vs-B-tree trade in one breath: sequential-write throughput bought with read amplification (check many places) and deferred cleanup (compaction).
- [ ] Why the memtable must be sorted (a flush is a sequential write of an already-sorted run — no re-sort, no random I/O).
- [ ] Tombstones from first principles: what a delete-as-removal would break the moment an older SSTable still holds the key.
- [ ] The freeze-and-swap handoff: why writes never block on a flush, and what bounds how many frozen memtables can pile up.
- [ ] SSTable anatomy and the one-block point-lookup path; why immutability + sorted order also give cheap range scans and lock-free reads.
- [ ] Why a crash mid-flush is safe (the WAL still holds those writes; the half-file is discarded by the footer/fsync ordering).

**Depth probes:**
- Sequence numbers: what do they disambiguate that "arrival order in the map" cannot (flush timing, duplicate keys across levels)?
- Why do writes cost the same whether the DB holds 1 GB or 1 TB, while reads don't? What restores read speed (Cards 4–5)?

**Trap:** reconciling the read path in the wrong order or skipping a level. A key updated in the memtable but present in an SSTable must resolve to the memtable — every "wrong value after flush" bug is an ordering bug here.

---

## 🧠 Card 4 — Bloom filters: skipping disk you never needed *(V5 · `src/bloom.rs`)*

**The problem.** A GET for an *absent* key is the LSM's worst case: it's not in the memtable, not in any of 30 SSTables — and proving each "not here" costs an index probe and possibly a block read, ×30, for nothing. Miss-heavy workloads (checking existence, cache-miss lookups) turn read amplification into a disk storm.

**The idea.** Per SSTable, a bloom filter: k hash functions setting bits in a small array. Query answers "definitely not present" or "maybe present" — the asymmetry is the entire design. False positives cost one wasted block read at a tunable rate (~1% at 10 bits/key); false *negatives* would silently hide data that exists on disk, which is why "no false negatives" is a structural guarantee, not a quality target. Most misses are now answered from memory without touching the file. The filter persists in the SSTable and reloads on open.

**In the wild:** every LSM engine ships bloom filters per file (RocksDB, Cassandra); also network dedup, CDN cache-existence checks, Chrome's (former) malware URL pre-check — anywhere "cheap probable-membership before an expensive lookup" pays.

**You own it when you can explain:**
- [ ] The mechanism (k hashes, bit array, all-bits-set test) and why deletion from a plain bloom is impossible.
- [ ] The asymmetry as design: what each answer licenses you to skip, and why the two error directions have wildly different costs *in a database*.
- [ ] The bits-per-key ↔ false-positive-rate curve, roughly, and what you'd tune for a miss-heavy workload.
- [ ] Why immutable SSTables make blooms trivial to maintain (build once at flush, never update) — mutability would break them.
- [ ] The proof shape: a filter-rejected key reads *zero* data blocks (counter-verified), and measured FP rate ≈ theory.

**Depth probes:**
- Blooms don't help range scans. Why (membership is per-key; ranges need order), and what does RocksDB use instead (prefix blooms)?
- Why is the bloom checked *before* the block index, and what's the cost ordering of the full lookup path?

**Trap:** a hash/serialization mismatch between build time and reload time producing rare false *negatives*. It looks like "the database occasionally loses a key" — one of the scariest bug reports a storage engine can get, from one of the smallest bugs.

---

## 🧠 Card 5 — Compaction: paying down write debt *(V6 · `src/compaction.rs`)*

**The problem.** Every flush adds an SSTable. Left alone: reads consult ever more files (read amplification grows without bound), overwritten values and tombstoned keys hold disk forever (space amplification), and eventually flushes outrun the disk and the engine does the one thing a database must never do — **stop accepting writes**. Real engines call it a write stall; it has paged real humans at real companies.

**The idea.** Background compaction merges sorted runs into fewer, larger immutable files, dropping shadowed values and reclaimable tombstones (a tombstone may only die when no older file beneath the merge still holds its key — drop it early and the key *resurrects*). The governing law is the **amplification triangle**: write amp (bytes rewritten by merging), read amp (files per lookup), space amp (dead data retained) — you cannot minimize all three; the compaction policy (size-tiered vs leveled) is precisely your choice of which two to favor. Debt must be bounded: under sustained load, L0 file count stays within a factor of its trigger, and compaction runs *without* collapsing foreground throughput.

**In the wild:** RocksDB leveled vs universal compaction (whole conference talks on tuning it), Cassandra's STCS/LCS/TWCS choices, project 20's segment merging (same idea, search flavor), Postgres VACUUM (same debt, MVCC flavor).

**You own it when you can explain:**
- [ ] The debt spiral mechanically: flushes add files → reads slow → compaction I/O competes with flushes → L0 grows → the stall — and where bounding breaks the loop.
- [ ] Tombstone reclamation's precondition, with the resurrection bug if violated (the merge drops the tombstone; an older SSTable outside the merge still has the key; the key returns from the dead).
- [ ] The triangle with each policy placed on it: size-tiered (low write amp, high read/space amp) vs leveled (high write amp, low read/space amp) — and a workload that wants each.
- [ ] Why compaction must be *background* work with bounded interference — what a stop-the-world merge would do to p99.
- [ ] What "newest value wins" requires the merge to know (sequence numbers across input files).

**Depth probes:**
- Write amplification arithmetic for leveled compaction: a key rewritten once per level across 5 levels costs what per user byte? Why do SSD endurance sheets care?
- What actually triggers a write stall in RocksDB (L0 file count thresholds → slowdown → stop) — and what's your equivalent?

**Trap:** tuning compaction on a bursty benchmark. Debt hides in bursts — the honest test is *sustained* load where compaction must keep pace indefinitely; that's why the boss fight is a full minute of firehose with the last-10s throughput compared to the first.

---

## 🧠 Card 6 — The block cache: a hand-built LRU under the read path *(V7 · `src/block_cache.rs`)*

**The problem.** Real workloads are skewed — the same hot blocks are read constantly. Without a cache, every hot read re-pays a block read + CRC + decode; with an *unbounded* cache you re-invent the OOM. And this cache sits under every concurrent connection, so a single coarse lock makes the cache itself the bottleneck.

**The idea.** Project 07's O(1) LRU (map + intrusive recency list), rebuilt where it counts: keyed by (file, block), **byte-bounded** (blocks vary in size — count bytes, not entries; overwrites must not double-count), safe under concurrent readers (sharding cuts contention), and cleanly disable-able (`BLOCK_CACHE_BYTES=0` → every read hits disk, nothing grows). One honest wrinkle to name: the OS page cache is *also* caching these file bytes — you're double-caching decoded vs raw forms, and whether to fight that (`O_DIRECT`) is a named decision, not an accident.

**In the wild:** RocksDB's block cache (its size is *the* RocksDB tuning knob), InnoDB's buffer pool, and the deliberate contrast with project 20's choice to just trust mmap + page cache — the two philosophies of read-memory management.

**You own it when you can explain:**
- [ ] Why the cache stores *decoded blocks* (skip read + CRC + decode on hit) and what keying by (file, block) assumes (immutability — a mutable file would need invalidation).
- [ ] Byte-bounding vs entry-bounding, and the double-count bug on overwrite.
- [ ] The concurrency design: what sharding by key hash buys, and what global accounting it complicates (same trade as project 07).
- [ ] The block-cache vs page-cache story: what's cached twice, what `O_DIRECT` changes, and why "let both cache" is often fine — as a decision you can defend.
- [ ] The proof metrics: hit ratio on a Zipfian read load, and zero disk block reads on a repeated hot key.

**Depth probes:**
- Why does a *scan* (range read) pollute an LRU block cache, and what do real engines do (separate scan path / midpoint insertion)?
- How would you decide the split between block-cache bytes and leaving RAM to the page cache on one box?

**Trap:** measuring the cache by hit ratio alone. A cache serving stale blocks after a compaction replaced the file would have a *great* hit ratio — immutability is what makes (file, block) keys safe, and that assumption deserves a test.

---

## ⚡ Rapid-fire round

- [ ] `AUTH`/`NOAUTH`/`WRONGPASS` semantics when `REQUIREPASS` is set — and the password never in logs or `/stats`.
- [ ] Request bounds: max bulk size rejected pre-allocation, `maxclients` connection cap — the DoS surface of an open TCP port.
- [ ] Graceful shutdown: stop accepting, finish in-flight commands, fsync the WAL — SIGTERM loses nothing acknowledged.
- [ ] The dashboard that tells the whole story: memtable bytes (flush pressure), L0 count (compaction debt), block-cache hit ratio, WAL fsync latency, per-command p99.
- [ ] Wrong-arity/unknown commands return `-ERR` and keep the connection alive — protocol errors aren't connection errors.
- [ ] Why `/stats` transitions (memtable falls on flush, SSTable count falls on compaction) are themselves a correctness check.

## 🔗 Connects to

- This keystone *is* the gauntlet folded together: project 08's log (WAL), 06's atomic commit (SSTable flush), 07's LRU (block cache), 19's framing (RESP), 20's immutable-segments-plus-merge (compaction) — one project per organ, now one organism.
- Project 20's mmap-and-trust-the-page-cache vs this project's hand-built block cache are the two answers to read memory — having built both, you can argue either side.
- The write-stall boss is the storage-engine version of every backpressure lesson since project 01: unbounded debt is always an outage on a delay.
