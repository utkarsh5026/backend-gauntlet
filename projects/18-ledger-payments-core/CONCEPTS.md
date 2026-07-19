# Concept Bank — Project 18: Ledger / Payments Core (Stripe-lite)

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted. This is the one domain where a race condition has a dollar figure attached — precision is the point.

---

## 🧠 Card 1 — Double-entry: the balance is a derivation *(V1 · `src/ledger.rs`)*

**The problem.** Model money as `UPDATE balances SET amount = amount - 10` and you've built a system that can't answer "why is this balance what it is?", can't detect when a bug silently created money, and has already lost the history an auditor (or your own 3 a.m. debugging) needs. A mutated number has no memory.

**The idea.** Five-hundred-year-old technology: every movement is a **transaction** of immutable **entries** whose signed amounts sum to exactly zero — money moves, it is never created or destroyed. A transfer of $10 is `-1000` on A and `+1000` on B (integer minor units — rule zero: no floats near money). A balance is *derived*: the sum of an account's entries. Corrections are new reversing transactions, never edits. The zero-sum invariant becomes machine-checkable at any moment: sum every entry in the ledger — anything but zero means a bug just got caught by arithmetic.

**In the wild:** Stripe's and every bank's core is a double-entry ledger; Square's and Uber's money systems (both have public writeups); this is also event sourcing (project 21) discovered by accountants centuries early.

**You own it when you can explain:**
- [ ] Why balance-as-derivation beats balance-as-mutable-cell on three axes: auditability, lost-history, and the concurrency story it sets up (V2).
- [ ] The zero-sum invariant as an executable audit: what a nonzero ledger sum proves, instantly, that no amount of logging proves.
- [ ] Why entries are immutable and corrections reverse — what an `UPDATE` on a posted entry would destroy.
- [ ] Rule zero, with the arithmetic: which exact values `f64` cannot represent, and how rounding errors compound into unreconcilable books.
- [ ] Atomic posting: why "all entries or none" is non-negotiable — a half-posted transaction *is* created/destroyed money.
- [ ] One currency per account, and what a cross-currency transfer actually requires (two transactions through an FX intermediary account — worth being able to sketch).

**Depth probes:**
- Debits, credits, and normal balances: map the accountant's vocabulary onto your signed integers — where do asset vs liability accounts differ?
- Deriving balances means summing entries forever. When does that get slow, and what's the safe fix (snapshot/checkpoint rows that are *derived and verifiable*, never authoritative)?

**Trap:** storing a running balance column "for performance" on day one. The moment two writers touch it, it drifts from the entries — and now you have two sources of truth voting about money. Derive first; cache later, verifiably.

---

## 🧠 Card 2 — Isolation levels: where the money bug lives *(V2 · `src/isolation.rs`)*

**The problem.** Two requests debit the same account simultaneously. Both `SELECT` the balance: 100. Both check `100 >= 60`: pass. Both post. Balance: −20, on an account you promised can't overdraft — and *nothing errored*. Under `READ COMMITTED` (Postgres's default) this is legal, invisible, and it costs real money. Both transactions were individually correct; the interleaving was the bug.

**The idea.** This is the lost-update/write-skew anomaly class, and the fix is choosing where serialization comes from: **`SERIALIZABLE`** (optimistic — Postgres detects the dangerous interleaving and aborts one transaction with a `40001`, which you *retry*) or **`SELECT … FOR UPDATE`** (pessimistic — the row lock forces the second transaction to wait and re-read). Either way, two disciplines: the invariant check lives *inside* the money-moving transaction (a pre-check outside it is exactly the race), and the retry loop is part of the design — serialization failures are expected operation, not errors to leak as 500s.

**In the wild:** every payments/banking system on an RDBMS; Postgres's SSI implementation; the write-skew examples in *Designing Data-Intensive Applications* (the on-call-doctors example is this bug in a hospital).

**You own it when you can explain:**
- [ ] The double-debit interleaving from memory, statement by statement, and why each transaction passes review in isolation.
- [ ] Lost update vs write skew, precisely — and which anomalies `READ COMMITTED`, `REPEATABLE READ`, and `SERIALIZABLE` each permit (the ladder, with one example per rung).
- [ ] Optimistic vs pessimistic: what each costs under low vs high contention on one hot account, and why the answer flips.
- [ ] Why `40001` retries must be bounded, jittered, and invisible to the client — and what surfaces when the retry budget exhausts.
- [ ] Money conservation under a storm as the system-level test: Σ balances unchanged to the cent across thousands of racing transfers — the invariant that catches whatever you didn't think of.

**Depth probes:**
- Why can't `REPEATABLE READ` (snapshot isolation) stop write skew? What is it about *two disjoint writes justified by overlapping reads* that snapshots miss?
- One celebrity account receives 40% of all transfers. Compare `FOR UPDATE` vs `SERIALIZABLE` throughput on it, and name one mitigation (sub-accounts/sharded balances) with its cost.

**Trap:** validating "sufficient balance" in application code before opening the transaction. It reads clean, passes every sequential test, and is precisely the TOCTOU race — the check must happen under the same isolation that commits the debit.

---

## 🧠 Card 3 — Idempotency keys: exactly-once effects on a retrying network *(V3 · `src/idempotency.rs`)*

**The problem.** A client POSTs a transfer; the connection times out. Did the money move? *The client cannot know* — the timeout might have hit before or after commit. Its only rational move is to retry — and an unprotected retry is a double charge. The network's at-least-once nature meets money, and money loses.

**The idea.** Stripe's pattern: the client sends an `Idempotency-Key`; the first request with that key executes and **stores its full response**; any replay returns the stored response without re-executing — same transaction id, same status, zero new postings. Three sharp edges make it real: the key is bound to a **request fingerprint** (same key + different body = conflict, not a stale answer); **concurrent duplicates** race safely (exactly one executes; the other waits or gets the same result); keys **expire** on a documented TTL. Redis caches replays; Postgres is the durable record — a cache miss re-reads, never re-executes.

**In the wild:** Stripe's `Idempotency-Key` header (their engineering post is canonical), PayPal request ids, SQS dedup ids — every serious payment API exposes exactly this contract.

**You own it when you can explain:**
- [ ] The client's epistemic bind on timeout (commit happened or not — indistinguishable) and why retry-with-key is the only correct client behavior.
- [ ] Idempotency as the bridge: at-least-once *delivery* (unavoidable) + keyed dedup = exactly-once *effect* (achievable) — and why the distinction is the whole vocabulary of reliability.
- [ ] Why the fingerprint check exists: the same key on a different body is a client bug that must surface as a conflict, not silently return someone else's result.
- [ ] The concurrent-duplicate race (both arrive before either finishes): what serializes them, and what the loser returns.
- [ ] The ordering decision — store the key-record before or after moving money — and which failure each ordering leaves you with (a reserved-but-unexecuted key vs a executed-but-unrecorded replay window).

**Depth probes:**
- Why does the *response* get stored, not just a "done" flag? (The replay must be indistinguishable — same body, same status.)
- What TTL is right, and what breaks at each extreme? (Too short: a late retry double-charges; too long: unbounded storage and stale conflicts.)

**Trap:** implementing idempotency in Redis alone. A cache eviction then means a replayed key *re-executes* — the dedup record is money-critical state, and money-critical state lives in the durable store.

---

## 🧠 Card 4 — The transactional outbox & signed webhooks *(V4 · `src/webhooks.rs`)*

**The problem.** A transfer settles; the merchant's system must hear about it. The naive version — commit the DB transaction, then fire the HTTP call — has a crash window between the two: money moved, notification lost, forever. Flip the order and you notify about money that never moved. This is the **dual-write problem**: two systems, no shared transaction, and every ordering loses. On top: the receiver must be able to *verify* the event is from you (anyone can POST JSON), and their endpoint will be down exactly when it matters.

**The idea.** The **transactional outbox**: enqueue the event as a row *in the same DB transaction as the posting* — now "posting exists" and "event exists" are atomically the same fact. A dispatcher delivers out-of-band with exponential backoff + jitter, dead-letters after a cap, and survives restarts because the outbox is durable. Authenticity: **HMAC-SHA256 over (timestamp, body)** with a shared secret — verifiable by the receiver, constant-time compared, timestamped against replay. And because delivery is at-least-once, the *receiver* needs idempotent handling — V3's lesson, now on the other side of the wire.

**In the wild:** Stripe webhooks (signature header, timestamp, retries over days, exactly this design); the outbox pattern is standard microservices literature (Debezium reads outboxes via CDC); GitHub/Shopify webhooks all HMAC-sign.

**You own it when you can explain:**
- [ ] The dual-write problem cleanly: both orderings of {commit, notify} and the crash window each leaves — then why the outbox dissolves it (one transaction, one fact).
- [ ] Why delivery must be async (the transfer response never waits on a merchant's server) and what the outbox row's lifecycle is (pending → delivered / dead).
- [ ] The signature scheme end to end: what's signed, why the timestamp is inside the signature (replay), why verification compares constant-time.
- [ ] Backoff + jitter + DLQ, imported from project 04 — and why "the receiver was down for six hours" must converge to delivered-or-dead-lettered, never a hot loop.
- [ ] Why at-least-once delivery makes receiver-side idempotency mandatory — the event id as *their* idempotency key.

**Depth probes:**
- Ordering: can event N+1 deliver before N (parallel dispatchers, retries)? Does your contract promise order, and what would promising it cost?
- Compare outbox-polling vs CDC (Debezium tailing the WAL) as dispatcher implementations — latency, load, and operational complexity.

**Trap:** "we'll enqueue to Kafka/Redis after commit instead" — that's the same dual-write with a different second system. The fix isn't a better queue; it's *one* transaction, which only the database the posting lives in can give you.

---

## ⚡ Rapid-fire round

- [ ] The status-code contract: `201` fresh post, `200` idempotent replay, `409`/`422` key conflict — the ledger's semantics visible in HTTP.
- [ ] Why balances are served from Postgres, never a cache — staleness on the invariant path is the one place cache-aside is wrong (contrast with project 01).
- [ ] Amount validation as money hygiene: non-positive, over-ceiling, currency-mismatch — each rejected with a test, no `f64` anywhere on the path (grep-provable).
- [ ] Graceful shutdown: drain in-flight transfers, let the dispatcher finish its batch — no half-delivered state.
- [ ] The metrics that watch money health: serialization-retry count (contention), idempotency hit ratio (client retry behavior), outbox lag (delivery health), DLQ depth.
- [ ] Secret hygiene: signing keys never logged; API keys rejected before the handler runs.

## 🔗 Connects to

- The append-only, state-is-derived ledger is project 21's event sourcing with money semantics — build both and the pattern is yours forever.
- Backoff/jitter/DLQ is project 04's retry machinery, re-earned on the webhook path.
- The isolation-level lesson is the SQL face of project 02's TOCTOU and project 04's claim race — the same interleaving bug in its most expensive costume.
