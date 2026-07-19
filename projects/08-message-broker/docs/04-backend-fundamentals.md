# Backend Fundamentals Woven Through This Project

> What this teaches: the horizontal-checklist ideas from [SPEC.md](../SPEC.md)
> that aren't owned by a single vertical — lag as *the* broker health metric,
> why every wire exchange is batched and bounded, the deliberate contention
> model, the security of filenames you didn't choose, graceful shutdown, and
> retention. Each is small; together they're the difference between "my log
> works" and "my broker is operable." No prior knowledge assumed; docs
> [00](00-the-append-only-log.md)–[03](03-consumer-groups-and-delivery-guarantees.md)
> supply the vocabulary.
>
> Anchored to [src/routes.rs](../src/routes.rs), [src/main.rs](../src/main.rs),
> [src/partition.rs](../src/partition.rs), [src/error.rs](../src/error.rs), and
> [CONCEPTS.md](../CONCEPTS.md)'s rapid-fire round.

---

## 1. Consumer lag — the one number that tells you the broker's truth

Every partition has two moving cursors, both already in the scaffold's
vocabulary:

- **log-end offset** — the next offset an append will get
  (`Log::log_end_offset`, [log.rs:132](../src/log.rs#L132)): how far
  *producers* have gotten.
- **committed offset** — a group's durable bookmark
  (`GroupCoordinator::committed`): how far *that group of consumers* has
  gotten.

**Lag = log-end − committed.** From doc 03's worked example: log-end 8,
`billing` committed at 3 → lag 5. It's the answer to "how far behind is the
consumer?", measured in records, per (group, partition).

Why is this *the* health metric — over CPU, memory, produce rate, anything?
Because it's the only number that couples both sides of the system:

| Lag behavior | What it tells you |
| --- | --- |
| ~0, stays there | Consumers are keeping up. The system is healthy *by definition*, whatever the CPU graphs say. |
| Spikes, then drains | A burst arrived; consumers absorbed it. Normal breathing. |
| **Climbs steadily, never drains** | Consume rate < produce rate — *permanently*. No amount of waiting fixes a rate imbalance. |

That last row forces a real decision, which is why the metric matters: scale
consumers out (until V3's partition-count ceiling stops you), make processing
faster, shed load — or accept that retention (§6) will eventually delete
records the group *never read*. Silent data loss, discovered weeks later, is
what unmonitored lag looks like.

The SPEC's observability checklist asks for exactly this at `/metrics`:
produce rate & bytes-in, per-partition log-end offset, and per-group lag —
plus structured log lines on the three events an operator greps for at 3 a.m.:
**segment roll**, **retention delete**, **group rebalance**.

---

## 2. Batched and bounded: the shape of every wire exchange

Look at the produce/fetch surface in [routes.rs](../src/routes.rs) — both
directions are plural, and that's load-bearing:

- **Produce takes `records: Vec<…>`**
  ([`ProduceReq`](../src/routes.rs#L81-L84)). Per-record HTTP requests would
  drown the actual work in per-request overhead (headers, JSON envelope,
  routing, a network round-trip each). One request carrying 500 records
  amortizes all of it — and hands V1's group-commit fsync (doc 00 §4) a
  natural batch to cover.
- **Fetch returns a *bounded* batch plus `next_offset`**
  ([`FetchQuery`](../src/routes.rs#L132-L139), default `max_records = 100`).
  The bound is not politeness — it's self-defense. An unbounded fetch at
  offset 0 of a 50 GB partition is a request to buffer 50 GB into one HTTP
  response; the storage-lifecycle checklist item says the same thing at the
  file layer (*stream from the segment, never buffer a whole segment in RAM
  per fetch*).

Bounded responses need a cursor to continue from — that's `next_offset`
([routes.rs:167](../src/routes.rs#L167)): last returned offset + 1, or, on an
empty batch (the tailing consumer at log-end, doc 01 §3), the requested offset
unchanged. The client loops: fetch → process → fetch from `next_offset`. This
request/cursor/bound triple is the universal shape of paginated streaming
APIs; you also built it as S3 list pagination in project 06.

The wire-format checklist item is the honest footnote: record bytes currently
ride as UTF-8 strings inside JSON (fine for `curl`, wrong for arbitrary
bytes — `String::from_utf8_lossy` on the fetch path will mangle non-UTF-8
values). Documenting that encoding, or moving to base64 / a length-prefixed
binary TCP protocol (stretch), is deliberate scope.

---

## 3. The contention model: one writer per partition, on purpose

[partition.rs](../src/partition.rs) is six lines of structure that encode the
whole concurrency design: each `Partition` holds its `Log` behind its own
`tokio::sync::Mutex`.

- **Appends to one partition serialize.** They must — `next_offset`
  assignment is what makes offsets a total order (doc 02 §1). The lock isn't a
  regrettable bottleneck; it *is* the ordering guarantee, held as narrowly as
  possible.
- **Different partitions don't contend.** One mutex per partition, so N
  partitions give N independent append lanes — this is what the SPEC's
  cross-cutting bench (throughput vs partition count) is designed to make
  visible.
- **The scaffold's honest simplification:** the same lock is currently held
  across *reads* too ([partition.rs:1-7](../src/partition.rs#L1-L7) says so
  out loud). Reads of sealed segments are reads of immutable files (doc 00
  §6) and could proceed concurrently; relaxing this is the "reads stay
  concurrent" horizontal item, and *knowing precisely why it's safe* — which
  bytes can no longer change — is the actual lesson.

The general principle, portable to every project after this one: don't let a
contention model *happen* to you. Decide where writes serialize, document it,
and bench the claim.

---

## 4. Security: you're letting strangers name your files

Two checklist items, one root cause: **client-supplied strings become disk
operations.**

**Topic names become directory names.** `Topic::create` does
`root.join(name)` ([topic.rs:46](../src/topic.rs#L46)). Feed it
`name = "../../../home/user/.ssh"` and a naive broker happily operates outside
its data directory — path traversal, the same attack class project 06's object
keys faced. Names need validation (an allowlist shape like
`[a-zA-Z0-9._-]+`, length-capped, with `.`/`..` rejected) *before* any
filesystem call, answered with
[`AppError::InvalidRequest`](../src/error.rs) (400), with a test proving the
evil names bounce.

**Record size is a disk-space grant.** Every accepted record consumes broker
disk. Without a cap, one client streams you out of storage — no exploit
needed, just a loop. The scaffold already enforces `MAX_RECORD_BYTES`
(default 1 MiB) on the produce path
([routes.rs:114-117](../src/routes.rs#L114-L117)) →
[`AppError::RecordTooLarge`](../src/error.rs) (413).

**And produce needs a credential.** An open produce endpoint *is* an open
disk — the TODO at [routes.rs:103-104](../src/routes.rs#L103-L104) marks
where auth belongs (before any work happens), and the checklist adds: never
log the credential.

---

## 5. Graceful shutdown: the fsync dial's last test

Doc 00 established that durability lives in `fsync`, on a policy with a
bounded un-synced window. A `SIGTERM` (every deploy, every `docker stop`)
is where that window gets cashed in: whatever was acknowledged but not yet
fsync'd is exactly what a hard exit loses.

Graceful shutdown is closing the window on purpose: stop accepting new work,
let in-flight appends finish, then fsync the active segments *and* any
uncommitted group offsets before exiting. The scaffold wires the mechanism —
`axum::serve(...).with_graceful_shutdown(shutdown_signal())` in
[main.rs](../src/main.rs#L92-L94) drains in-flight requests — and leaves the
flush-and-fsync as the marked TODO on `shutdown_signal`
([main.rs:101-103](../src/main.rs#L101-L103)). The checklist criterion is
observable: a clean restart finds **no torn tail and no lost cursor** — i.e.
recovery (doc 00 §5) finds nothing to repair.

---

## 6. Retention: V1's payoff, operationally

Segments exist (doc 00 §6) so this item can be one line of policy: past a
size or age bound, **delete the oldest whole segment files** — never rewrite
a live one. `rm` per segment, zero interaction with the writer (which only
touches the last segment), observable as segment count dropping under load,
logged as a structured event (§1). The V1 stretch goal is the background
worker that applies it.

Its one sharp edge is the interaction with §1: retention doesn't know about
committed offsets. A group lagging further than the retention window loses
records unread — which is precisely why lag is the metric you alarm on, and
why "climbing lag" is a decision, not a curiosity.

---

## 7. Mental model summary

| Fundamental | One-line takeaway |
| --- | --- |
| Consumer lag | log-end − committed, per (group, partition): the only metric coupling producers to consumers; steadily climbing lag forces a scaling decision. |
| Batched + bounded wire | Batch to amortize per-request cost; bound so no response is "the whole log"; `next_offset` is the continuation cursor. |
| Single writer per partition | The append lock *is* the ordering guarantee — held per-partition so parallelism survives; concurrent reads are the deliberate relaxation. |
| Names & sizes are attack surface | Client strings become paths (validate before touching disk) and disk grants (cap record size); produce sits behind a credential. |
| Graceful shutdown | Drain, then fsync segments + offsets: a clean restart finds nothing to recover. |
| Retention | Delete whole sealed segments — O(1), writer-free — and let lag monitoring protect slow readers from it. |

**Where these land:** mostly thin, deliberate additions around code that's
already wired — validation and auth in [routes.rs](../src/routes.rs), the
shutdown TODO in [main.rs](../src/main.rs#L101-L103), metrics via
`common-telemetry`, the retention worker as V1's stretch. They're the
horizontal checklist's own checkboxes in [SPEC.md](../SPEC.md) (Protocols,
Storage lifecycle, Security, Observability, Cross-cutting), each needing its
observable proof — and several feed the `bench/` numbers the Definition of
done requires.
