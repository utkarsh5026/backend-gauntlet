# Bounded Caches and O(1) Eviction — From First Principles

> What this teaches: why a real cache needs a *hard capacity*, why picking an
> eviction victim in O(1) forces a second data structure beside the hash map, and
> how LRU and LFU disagree about who to throw out. No prior knowledge assumed.
> Prepares you for **V1** in [SPEC.md](../SPEC.md) — the store you'll build in
> [store.rs](../src/store.rs), where `get`, `put`, `remove`, and `len` are
> currently `todo!()`.

---

## The one sentence to hold onto

**A bounded cache is two data structures pretending to be one: a map that answers
"where is this key?" in O(1), and an ordering index that answers "who should die
next?" in O(1) — and every operation must keep them in perfect agreement.**

---

## 1. The problem: an unbounded `HashMap` is an OOM on a timer

The naive cache is beautiful:

```rust
let cache: HashMap<String, Bytes> = HashMap::new();
```

`get` is O(1), `put` is O(1), done in five minutes. It has exactly one flaw: it
only ever grows. Every distinct key ever written stays forever. On a node with
8 GiB of RAM serving a keyspace of millions of session tokens, user profiles, and
rendered fragments, this isn't a cache — it's a memory leak with an API.

The fix sounds trivial: **cap it**. `docker-compose.yml` in this project sets
`CACHE_CAPACITY: 100000` — at most 100,000 live entries per node, and
[store.rs](../src/store.rs) receives that as `Store::new(capacity, policy)`.

But the cap creates a brand-new question that dominates the whole design:

> The store is full. A `put` arrives for a new key. **Which existing entry do you
> delete to make room?**

That question has two halves, and they're graded separately in the SPEC:

1. **Mechanism** — how do you *find and remove* the victim fast?
2. **Policy** — how do you *choose* a victim that hurts the hit ratio least?

## 2. Why the obvious mechanism is O(n) — and why that's fatal

Say the policy is "evict the least recently used" (LRU). The obvious
implementation: stamp each entry with `last_accessed: Instant`, and when full,
scan the whole map for the oldest stamp.

| Entries | Work per `put` into a full store | At 50k puts/sec |
|---|---|---|
| 1,000 | scan 1,000 entries | annoying |
| 100,000 | scan 100,000 entries | each put touches 100k entries — the store spends its life choosing victims |
| 1,000,000 | scan 1,000,000 entries | the cache is slower than the database it fronts |

A cache exists to be *fast*. An O(n) victim scan means the fuller the cache gets
(i.e., the better it's doing its job), the slower every write becomes. That's why
the very first V1 criterion says:

> `get` and `put` are **O(1)** — no scan of the keyspace to serve a read or to
> pick a victim.

## 3. The structural insight: you need a second index

Think about what "find the least recently used entry in O(1)" requires. The hash
map is organized by *key* — it can find `"user:42"` instantly, but it has no idea
which entry is oldest. Asking a hash map for "the minimum by recency" is like
asking a phone book for "the person who moved in most recently": the book is
sorted by the wrong thing, so you must read all of it.

The only way out is to maintain the answer **incrementally**: a second structure,
ordered by *eviction priority*, updated a little on every operation so that "who
dies next?" is always sitting at a known position.

For LRU, the classic shape (named in the repo's own
[CONCEPTS.md](../CONCEPTS.md) and in the module docs of
[store.rs](../src/store.rs)) is:

```
        HashMap (find by key, O(1))
        ┌──────────┬─────────────────┐
        │ "user:42"│ ──┐             │
        │ "hello"  │ ──┼──┐          │
        │ "sess:9" │ ──┼──┼──┐       │
        └──────────┴───┼──┼──┼───────┘
                       ▼  ▼  ▼
   recency order (a doubly-linked list threaded through the entries)

   front (MRU)  ◀──▶  "sess:9"  ◀──▶  "user:42"  ◀──▶  "hello"   (LRU) tail
                                                          ▲
                                              evict victim: pop the tail, O(1)
```

Every operation touches both structures:

| Operation | Map does | Ordering index does |
|---|---|---|
| `get(k)` **hit** | find entry O(1) | unlink entry, splice to front (O(1) — this is why the list is *doubly* linked: you can unlink from the middle without a scan) |
| `put(k)` new, full | insert O(1) | pop the tail = victim, remove victim from **the map too**, push new entry to front |
| `put(k)` overwrite | replace value | splice to front (no eviction — the SPEC's `put` doc comment is explicit about this) |
| `remove(k)` | delete O(1) | unlink O(1) — [store.rs](../src/store.rs)'s `remove` TODO warns: forget this and the two structures **disagree**, which is the classic corruption bug in hand-rolled LRUs |

LFU keeps the same map but swaps the recency list for a *frequency* index
(entries grouped by how often they've been touched; the victim comes from the
lowest-frequency group). The interesting engineering question — which the SPEC
deliberately leaves to you — is how to keep *that* O(1) too, and how to break
ties within a frequency.

**Why a `get` must write.** Notice that in both policies a *read* mutates
bookkeeping (recency position, frequency count). This has two consequences the
scaffold already warns you about:

- The V1 criterion "a key you keep reading is not the one thrown out" is a test
  for exactly this. If your `get` doesn't touch the eviction state, your LRU is
  secretly FIFO — insertion order, not access order — and your hottest key can be
  evicted while being read a thousand times a second.
- `Store::get` takes `&self` but must mutate ([store.rs](../src/store.rs) says
  so out loud): a plain `RwLock` read guard isn't enough, because *a get writes*.
  That drives the locking decision below.

## 4. The policy question: LRU and LFU genuinely disagree

Recency and frequency are different theories about the future:

- **LRU** bets: *what you touched recently, you'll touch again* (temporal locality).
- **LFU** bets: *what you touch often, you'll touch again* (long-term popularity).

Here's a concrete trace where they evict **different keys** — this is the shape
of divergence the V1 criterion asks your test to demonstrate. Capacity 3:

| Step | Access | Cache after | Frequencies | Recency (MRU → LRU) |
|---|---|---|---|---|
| 1–3 | `A A A` | {A} | A:3 | A |
| 4 | `B` | {A,B} | A:3 B:1 | B, A |
| 5 | `C` | {A,B,C} | A:3 B:1 C:1 | C, B, A |
| 6 | `B` | {A,B,C} | A:3 B:2 C:1 | B, C, A |
| 7 | **`put D`** (full!) | ? | | |

- **LRU's victim: `A`** — least recently touched (nothing since step 3).
- **LFU's victim: `C`** — least frequently touched (count 1 vs A's 3, B's 2).

LRU just threw away the most popular key in the cache because it had a quiet
moment. LFU protected it. But flip the workload — a key that was hot *yesterday*
and is dead now — and LFU is the one that clings to a corpse (its high historical
count shields it) while LRU correctly lets it age out. Neither policy wins in
general; that's why the SPEC makes the policy **swappable** behind one interface
(the `EvictionPolicy` enum in [store.rs](../src/store.rs) already exists for
this) and asks you to *measure* the disagreement.

**The scan-pollution trap (why LRU's weakness matters in practice).** Imagine a
nightly export job that reads a million cold keys exactly once. Under LRU, every
one of those touches is "recent", so the scan evicts your entire hot working set
to make room for keys nobody will read again. This is why modern caches
(Caffeine's TinyLFU) add an **admission filter**: a newcomer must prove it's
likely to be re-read before it's allowed to evict an incumbent. That's the V1
stretch goal — worth understanding even if you don't build it.

## 5. TTL: expiry without a sweeper thread

Entries can carry a time-to-live (`PUT /cache/{key}?ttl=30`). The scaffold's
`Entry` type already models this: `expires_at: Option<Instant>` with an
`is_expired(now)` helper. The V1 criterion is carefully worded:

> an entry past its TTL is not returned by `get` and does not count as a live
> entry — **expiry is observable without an external sweep**.

The cheap design is **lazy expiry**: nobody removes an expired entry on a timer;
instead, a `get` that finds one treats it as a miss *and drops it on the spot*
(the doc comment on `Store::get` points at exactly this). Compare:

| Approach | Cost | Weakness |
|---|---|---|
| **Lazy (check on read)** | zero background work; expiry cost paid by the request that discovers it | an expired key nobody reads again **strands memory** until capacity pressure happens to evict it |
| **Background sweeper** | reclaims memory promptly | a periodic O(n) scan — the very thing V1 bans from the hot path — plus a tuning knob (sweep interval) |
| **Hybrid (what Redis does)** | lazy + a *sampled* sweep (check ~20 random keys per tick) | probabilistic, but bounds both the stranded memory and the per-tick cost |

Lazy alone satisfies the SPEC. Knowing *what it strands* is the part
[CONCEPTS.md](../CONCEPTS.md) asks you to be able to explain. One subtlety it
creates: `len()` must report **live** entries (it backs both the capacity
invariant test and the per-node key-count metric), so decide what "expired but
not yet discovered" means for your count and be consistent about it.

## 6. Concurrency: the documented decision

The store is shared by every request handler (`Arc<Store>`, methods on `&self`).
Since even `get` writes bookkeeping, the simplest correct design is:

```
one Mutex around everything (map + ordering index together)
```

That is *correct* — and V1's criterion only demands correctness plus a
**documented decision** on granularity. But one global mutex means 16 tokio
worker threads serialize on a single lock; your multi-core node performs like a
single-core one under contention. The classic escape is **sharding**: split the
keyspace into S independent sub-stores by key hash, each with its own lock —
threads touching different shards never contend.

Sharding isn't free, and the tradeoff is exactly what CONCEPTS.md's depth probe
asks about:

| | One big lock | Sharded locks |
|---|---|---|
| Contention | every op serializes | ~1/S of ops collide |
| Global `len()` | trivial | sum across S locks (or a separate atomic counter you must keep honest) |
| Capacity + eviction | one global cap, one victim ordering | per-shard caps (a hot shard evicts while a cold shard has room) or cross-shard eviction (hard) |
| LRU accuracy | exact | per-shard — "global least recent" no longer exists |

Neither answer is wrong. The SPEC wants you to **pick one on purpose, measure it
(the bench asks for single-threaded *and* N-thread contended throughput), and
write the choice down** in `docs/07-design.md`.

## 7. One trap before you benchmark

Don't evaluate your policies with uniform-random keys. Real cache traffic is
**Zipfian** — a few keys get most of the traffic (the viral post, the logged-in
celebrity). Under uniform access every key is equally (un)likely to be re-read,
so *no* policy can look good and LRU vs LFU appear identical. The interesting
differences only show up under skew. CONCEPTS.md flags this as the Card 1 trap;
keep it in mind when you write the bench for `docs/07-benchmarks.md`.

## 8. Mental model summary

| Question | Answer to hold onto |
|---|---|
| Why cap the cache? | An unbounded map is an OOM on a timer; the cap turns "store everything" into "store the *right* things" |
| Why does O(1) eviction need a second structure? | The map is indexed by key; "who dies next" is a question about *ordering*, and the only O(1) answer is one you maintain incrementally |
| Why must `get` mutate? | Recency/frequency **is** access history; a read that doesn't record itself makes the policy blind (LRU degenerates to FIFO) |
| LRU vs LFU in one line | LRU bets on recency, LFU bets on popularity; they evict different keys on the same trace, and workload shape decides the winner |
| Lazy TTL in one line | Expiry is checked at read time — no sweeper on the hot path, at the cost of stranded memory for never-read-again keys |
| Locking in one line | One lock is correct; sharded locks buy throughput and cost you global invariants (size, true LRU order) — decide and document |

## 9. Where you'll build this

Everything lands in [store.rs](../src/store.rs):

- the state field(s) inside `Store` (the `TODO(V1)` comment marks the spot —
  map + ordering index behind your chosen lock granularity),
- `get` / `put` / `remove` / `len` (all `todo!()` right now — the first real
  `GET`/`PUT` through [routes.rs](../src/routes.rs) will panic on them; that
  panic is the worklist),
- the tests sketched at the bottom of the file: capacity invariant (property
  test), LRU-vs-LFU victim divergence, TTL-as-miss, hot-key-survives.

This doc unlocks the six **Done when ALL true** boxes of **V1** in
[SPEC.md](../SPEC.md), plus the horizontal "Eviction policy with a hard capacity
and honoured TTLs" box. The design decisions you must make (and record in
`docs/07-design.md`): the ordering-index shape for each policy, the tie-break
rule for LFU, what `len()` means under lazy expiry, and one-lock vs sharded.
When you're stuck *implementing*, that's what `/hint` and `/quest` are for —
this doc's job ends at the door.
