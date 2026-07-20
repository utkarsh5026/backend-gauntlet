# How Loom and Shuttle Work — From First Principles

> A beginner-friendly, ground-up guide to **concurrency model checkers**: tools
> that replace the OS thread scheduler with a fake one so race bugs show up on
> purpose. No prior knowledge of formal methods assumed — but it helps if you
> already know this project's GC ↔ in-flight-PUT story (blob durable first, then
> index pointer; GC must not reap a blob that only a `tmp/` entry still names).
>
> This teaches the **concept** and wires tiny demos you can run. It does **not**
> implement the From-the-field backlog item (putting that race under a model
> checker) — that stays yours if you adopt it.
>
> Anchored to: [`tests/loom_shuttle_intro.rs`](../tests/loom_shuttle_intro.rs),
> [`SPEC.md`](../SPEC.md) (From the field), [`RESEARCH.md`](../RESEARCH.md)
> §Part 2 & 8 (ShardStore / lightweight formal methods).

---

## 0. The one sentence to hold onto

**Loom and Shuttle control *which thread runs next* inside a test, so you explore
many interleavings of concurrent code instead of hoping the OS happens to hit
the buggy one.**

Ordinary `cargo test` uses real OS threads. The kernel picks a schedule almost
at random. A race that needs GC to run *exactly* between "blob committed" and
"index pointer renamed" may never fire on your laptop — and then fire in
production. These crates make the schedule a first-class input to the test.

---

## 1. The problem: schedules hide bugs

Two threads, shared state, no bug *in the lines you stare at* — only in the
*order* those lines run:

```
Thread A (PUT):   commit blob → write tmp/ → rename into index/
Thread B (GC):    scan live pointers → scan tmp/ → delete unreferenced blobs
```

If B's sweep lands after A's blob commit but before A's `tmp/` is visible (or
before rename, depending on your guard), B can delete bytes A still needs. Your
unit tests that plant a finished `tmp/` entry and then call `gc()` once never
see that window — they are single-threaded stories about a finished state, not
concurrent stories about a moving one.

**The world before a model checker:** "we reasoned about the race and wrote a
graceful sequential test." That is necessary. It is not the same as exercising
the interleavings.

---

## 2. Loom vs Shuttle (the ShardStore split)

Amazon's ShardStore paper (SOSP 2021 — see [`RESEARCH.md`](../RESEARCH.md)
§Part 2) used both, for different sizes of problem:

| | **Loom** | **Shuttle** |
| --- | --- | --- |
| Strategy | Exhaustive (bounded) exploration of interleavings | Randomized schedules over many iterations |
| Guarantee | If the model finishes, every explored schedule was OK | "Didn't find a bug in N runs" — strong search, not a proof |
| Scale | Tiny models (few threads, few sync points) | Larger stress tests Loom cannot finish |
| Crate | [`loom`](https://docs.rs/loom) | [`shuttle`](https://docs.rs/shuttle) |

Same *idea* (fake scheduler). Different *coverage / cost* tradeoff. This
project pins both as **dev-dependencies** so you can learn each shape; the SPEC
item asks for one of them on the GC race, not both.

They are **not** the same as:

- **`proptest` / reference model** — random *inputs* and comparing to a model.
  Loom/Shuttle are about *thread order*.
- **DST (FoundationDB / TigerBeetle)** — whole-system deterministic simulation
  with fault injection. Heavier; related family of ideas (RESEARCH §Part 8).

---

## 3. The API shape (why you cannot use `std::sync`)

Inside a Loom or Shuttle test you must use **that crate's** stand-ins for
threads and sync primitives:

```rust
// Loom
use loom::sync::{Arc, Mutex};
use loom::thread;
loom::model(|| { /* spawn, lock, join */ });

// Shuttle
use shuttle::sync::{Arc, Mutex};
use shuttle::thread;
shuttle::check_random(|| { /* same shape */ }, 1000);
```

Those types look like `std`, but every `lock` / yield / atomic load is a
**scheduling point** the checker can reorder. If you mix in real
`std::thread::spawn` or `std::sync::Mutex`, the checker cannot see those events —
you are back to hoping the OS cooperates.

Rule of thumb: one test file (or module) per tool; never share one `Arc` type
across both without an abstraction layer. The intro demos keep Loom and Shuttle
in separate functions for that reason.

---

## 4. What the demos do

[`tests/loom_shuttle_intro.rs`](../tests/loom_shuttle_intro.rs) has two passing
tests with the same story:

1. Share a counter behind a mutex.
2. Spawn two threads; each increments once.
3. Join; assert the counter is `2`.

- **`loom_mutex_protects_counter`** — `loom::model` runs **every** interleaving
  of those sync points. If the mutex were missing, Loom would eventually hit a
  lost update and fail the assertion.
- **`shuttle_mutex_protects_counter`** — `shuttle::check_random(..., N)` samples
  many random schedules. Same assertion; weaker guarantee, same teaching shape.

There is also an **`#[ignore]`** buggy variant you can run on purpose:

```bash
cargo test -p object-store --test loom_shuttle_intro \
  loom_buggy_unsynchronized_counter -- --ignored
```

That one *should fail* (or Loom should report a permutation that loses an
update). Use it once so you trust the tool before you build a real model.

### Run the happy path

```bash
cargo test -p object-store --test loom_shuttle_intro
```

---

## 5. Your open exercise (SPEC From the field)

The backlog item stays open (`[~]`):

> The GC ↔ in-flight-PUT race under a model checker: the stated resolution is
> exercised with Loom (exhaustive) or Shuttle (randomized) interleavings, not
> just reasoned about.

You already have sequential proofs in [`src/index.rs`](../src/index.rs) (e.g.
`gc_keeps_a_blob_referenced_only_by_an_in_flight_temp`). The model-checker
exercise is different: an **in-memory** stand-in for the shared state (live
digests, `tmp/` / in-flight set, committed pointers) where concurrent PUT and GC
steps are scheduled by Loom or Shuttle, and your invariant never breaks — for
example, GC never removes a digest still named by a version pointer or an
in-flight `tmp` entry.

**Why it pays off more than when V3 first landed:** object **versioning** added
more pointer states (overwrite = new version + tip flip; delete markers), and
the **scrubber** is another concurrent actor (re-hash, quarantine, notify on
commit). More actors ⇒ more interleavings that hand-wavy reasoning misses —
exactly when exhaustive (Loom) or aggressive randomized (Shuttle) checking
earns its keep.

Done when you can point at a test that drives those actors under a checker and
asserts the observable invariant. Outcomes only — no prescribed algorithm here.
When it lands, tick the SPEC box and add a one-line proof path (same style as
the scrubbing / durability-review items).

---

## 6. Practical tips when you start the exercise

1. **Shrink the world.** Model digests as `u8` or `&'static str`, not SHA-256
   hex. Two keys, one shared blob is enough to talk about dedup + GC.
2. **Count sync points.** Each mutex lock is a branch in Loom's tree. Prefer a
   few coarse locks over many atomics until the model is correct.
3. **Start with Loom** on the smallest race (PUT vs GC only). Add scrubber /
   versioning steps once that passes — or switch that larger model to Shuttle.
4. **Do not drive the real axum/`Index` under Loom.** Disk and Tokio are outside
   Loom's memory model. The checker wants a pure concurrent sketch of the
   *protocol*, not the production I/O path.
5. **Keep the sequential tests.** Model checking complements
   `gc_keeps_a_blob_referenced_only_by_an_in_flight_temp`; it does not replace
   it.

---

## Further reading

- Loom book / docs: <https://docs.rs/loom>
- Shuttle docs: <https://docs.rs/shuttle>
- Bornholt et al., *Using Lightweight Formal Methods to Validate a Key-Value
  Storage Node in Amazon S3* (SOSP 2021) — the industrial template behind this
  project's RESEARCH notes.
- This project's durability threat list (related, not a substitute):
  [`07-durability-review.md`](07-durability-review.md).
