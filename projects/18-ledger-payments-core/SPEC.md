<!-- status:
state: not-started      # active | paused | blocked | done | not-started
blocked-on: ~           # free text, or ~ for none
-->

# Project 18 — Ledger / Payments Core *(Stripe-lite)*

> Moving money is the one place in backend where a race condition has a dollar
> figure attached. A ledger looks like CRUD — insert a row, read a balance — until
> two requests touch the same account at the same instant and one of them quietly
> spends money that isn't there. This project is where **isolation levels stop
> being interview trivia**: the same code is correct under `SERIALIZABLE` and
> silently wrong under `READ COMMITTED`, and the only way to *know* is to build the
> invariant and then attack it with concurrency. On top of that sits everything a
> real payments core needs to be trustworthy: an append-only double-entry ledger
> that can never lose a cent, idempotency so a client's retry doesn't double-charge,
> and signed webhooks so the outside world hears about a settlement exactly once —
> reliably, even across a restart.

## What it does (the easy part)
- `POST /accounts` → create an account (name, currency, overdraft policy).
- `POST /transfers` with an `Idempotency-Key` header → move money A → B, return the
  transaction. A replayed key returns the **same** result without moving money twice.
- `GET /accounts/{id}/balance` → the account's balance, derived from its entries.
- `GET /transactions/{id}` → a posted transaction and its entries.
- Every settled transfer fires a **signed webhook** to the merchant's endpoint.
- API-key auth on the write endpoints; balances/transactions read behind the same key.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists. Money is always **integer minor units** (cents), never
> a float — that's rule zero, not a challenge.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Double-entry ledger core — *the posting engine*
A balance is not a number you increment. In a real ledger a balance is **derived**:
it's the sum of an account's immutable entries. Every money movement is a
**transaction** — a set of entries whose signed amounts sum to exactly zero (money
is conserved: it moves, it's never created or destroyed). A transfer of $10 from A
to B is two entries: `-1000` on A, `+1000` on B. Build the posting engine in
`src/ledger_payments_core/ledger.py`: create accounts, and post a balanced transaction
**atomically**.

**Done when ALL true:**
- [ ] A posted transaction is **all-or-nothing**: either every entry lands or none do — there is no partial post, even if the process dies mid-write.
- [ ] **Every transaction balances:** the signed amounts of its entries sum to exactly zero — an unbalanced draft is rejected *before* it touches the ledger.
- [ ] **Entries are immutable:** no code path updates or deletes a posted entry; a correction is a *new* reversing transaction, not an edit.
- [ ] **Balance is derived, not stored-and-mutated:** an account's reported balance equals the sum of its entries, recomputed from the entry log — the two never disagree.
- [ ] **One currency per account:** every entry on an account matches the account's currency; a cross-currency posting is rejected.

**Proof:** a hypothesis property test that random *balanced* batches keep
`Σ entries == 0` both per-transaction and across the whole ledger; a test that an
unbalanced draft is rejected and writes nothing; a test that reported balance ==
recomputed-from-entries for every account. `docs/18-design.md` states the sign
convention (debit/credit).

*Concept to internalize:* double-entry bookkeeping as an **append-only, immutable
event log**; why a derived balance beats `UPDATE balances SET x = x - n` (auditability,
no lost history, and it sets up the concurrency story in V2).
**Stretch:** account `kind` (asset/liability/…) and a normal-balance sign per kind.

### V2. The balance invariant under concurrency — *where isolation levels bite*
Here's the bug that costs money: two transfers debit the same account at the same
instant. Both read balance = `100`, both approve a `60` debit, both commit — and the
account is now at `-20`, an overdraft you *promised* couldn't happen. Nothing errored.
This is the **lost-update / write-skew** class, and under `READ COMMITTED` it is
invisible. Fix it in `src/ledger_payments_core/isolation.py`: make the transfer **safe
under concurrency** — hold the no-overdraft invariant and conserve total money no
matter how many requests race.

**Done when ALL true:**
- [ ] A **no-overdraft** account can never go below zero, even under N concurrent debits racing on that one account — checked *and enforced* within the money-moving transaction, not before it.
- [ ] **Conflicting concurrent transfers are serialized:** one wins, the other retries or is cleanly rejected — never do both silently succeed and break the invariant.
- [ ] **Serialization failures are handled, not leaked:** a `40001`-class conflict is retried with a bounded number of attempts, not surfaced to the client as a 500.
- [ ] **Money is conserved under a storm:** after a burst of concurrent transfers, `Σ` of all balances is unchanged to the cent (money only moved).
- [ ] The isolation strategy is a **documented decision** — `SERIALIZABLE` vs `SELECT … FOR UPDATE` row locks — with the tradeoff named.

**Proof:** a concurrency test firing K parallel transfers (`asyncio.gather`, each on
its own connection) at one hot account and asserting (a) the balance never goes
negative and (b) total money is conserved; a test that a doomed transfer retries then
fails cleanly rather than 500-ing; `docs/18-design.md` names the isolation choice and
why.

*Concept to internalize:* isolation levels (`READ COMMITTED` → `REPEATABLE READ` →
`SERIALIZABLE`), **write skew**, and how the right level turns a silent money bug
into a *retryable* error you can catch — the retry loop is part of the design.

### V3. Idempotency keys — *exactly-once effects over an at-least-once network*
Clients retry. A timeout on `POST /transfers` tells the client nothing about whether
the money moved, so it resends — and without protection you've now charged twice.
Stripe's answer is the `Idempotency-Key` header: the first request with a key does the
work and **stores its response**; any replay of that key returns the stored response
without re-executing. Build it in `src/ledger_payments_core/idempotency.py`, cached in
Redis with Postgres as the durable source of truth.

**Done when ALL true:**
- [ ] Two requests with the **same key** produce **exactly one** money movement; the replay returns the *same* response (same transaction id, same status code).
- [ ] The stored result is **cached** (Redis) for fast replay, with Postgres as the durable record — a cache miss re-reads the stored result and **still never re-executes**.
- [ ] **Same key + a different request body** is a **conflict** (rejected), not silently served the old result — the key is bound to the request that created it.
- [ ] **Concurrent** duplicate submissions race safely: exactly one executes, the other waits for / returns the same result — never two postings.
- [ ] Keys **expire** on a defined TTL; an expired key is treated as new, and that window is documented.

**Proof:** a test that replaying a key returns the cached response and the ledger
shows **one** transaction; a concurrent double-submit test proving a single posting;
a same-key-different-body conflict test. `docs/18-design.md` records the TTL and the
"stored before or after the money moves?" ordering decision.

*Concept to internalize:* idempotency as the bridge from **at-least-once delivery** to
**exactly-once effect**; the request-fingerprint check; and the nasty race where two
identical requests arrive before either has finished.

### V4. Signed webhooks with retries — *tell the outside world, reliably*
When a transfer settles, the merchant needs to know — and their endpoint may be down,
slow, or hostile. Three things have to be true at once: the notification is **provably
from you** (an HMAC signature the receiver can verify, timestamped to defeat replay),
it is delivered **at least once** with backoff (down ≠ lost), and it **survives a
process restart** (the event outlives the request that made it). Build it in
`src/ledger_payments_core/webhooks.py` using the **transactional outbox** pattern.

**Done when ALL true:**
- [ ] A settled transfer enqueues **exactly one** webhook event **in the same DB transaction as the posting** — no event without a posting, no posting without an event (the outbox invariant).
- [ ] Delivery happens **out of band**: the `POST /transfers` response does not wait on the receiver's HTTP call.
- [ ] Each delivery carries an **HMAC-SHA256 signature** over `(timestamp, body)`; a receiver with the shared secret can verify it, and a **tampered** body fails verification.
- [ ] A failing delivery is **retried with exponential backoff + jitter** up to a cap, then **dead-lettered** — a flapping receiver eventually gets it or it lands in the DLQ, never an infinite tight loop.
- [ ] Delivery **survives a restart:** events still pending in the outbox are picked up after the process comes back — nothing is lost with the request that created it.

**Proof:** a test that a down receiver produces growing retry delays then a DLQ row;
a signature verify-then-tamper test; an outbox test that a posting and its event
**commit atomically** (kill one, you lose both). `docs/18-design.md` explains why the
outbox beats "just fire the HTTP call after commit".

*Concept to internalize:* the **transactional outbox** (enqueue the event in the money
transaction, deliver asynchronously), **HMAC request signing** with a timestamp, and
why at-least-once delivery forces the *receiver* to be idempotent too (V3 comes back).

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] **Deliberate status codes:** `POST /transfers` returns `201` on a fresh post, `200` on an idempotent replay, and a `409`/`422` on a key conflict — each verifiable in the response. *(Proof: route tests asserting each status.)*
- [ ] **`Idempotency-Key` is a first-class header**, read and enforced by a FastAPI dependency before the handler moves money. *(Proof: idempotency tests.)*
- [ ] Outbound **webhook** is a signed HTTP `POST` with `X-Signature` + `X-Timestamp` headers the receiver can verify. *(Proof: V4 signature test.)*
- [ ] **Graceful shutdown** drains in-flight transfers (uvicorn's graceful shutdown) *and* lets the webhook dispatcher finish its current batch (the FastAPI lifespan) on SIGTERM — no half-delivered state, no dropped in-flight posting.

### Caching
- [ ] Idempotency responses are **cache-aside** in Redis (V3) with Postgres as source of truth; a cache miss degrades to Postgres, never to re-execution.
- [ ] **Balances are authoritative from Postgres, not cached** — `docs/18-design.md` states this and *why* caching a balance is dangerous (staleness on the invariant path), naming what you'd need to cache one safely.

### Security
- [ ] **API-key auth enforced** on write/read routes (`src/ledger_payments_core/auth.py`, a FastAPI dependency): a request without a valid key is rejected before the handler runs, and keys never appear in logs or error bodies. *(Proof: reject test.)*
- [ ] **Amount validation:** transfers reject non-positive amounts, amounts over a configured ceiling, and mismatched currencies — each with a test. Money is integer minor units end to end; there is **no `float` on the money path**. *(Proof: validation tests + a grep for `float`.)*
- [ ] **Webhook secret hygiene:** the signing secret never appears in logs; signature **verification uses a constant-time compare**. *(Proof: `docs/18-design.md` + verify test.)*
- [ ] **No SQL injection:** every query is parameterized (`$1` placeholders through asyncpg) — zero string-built SQL.

### Observability
- [ ] A structured log line per request carrying a request id (via `common_telemetry.RequestIdMiddleware`). *(Proof: a request log carrying the id through to the posting.)*
- [ ] Each transfer logs **from/to account, amount, transaction id, and serialization-retry count** as structured fields.
- [ ] Counter/gauge metrics at `/metrics`: **transfers/sec, serialization-retry count, idempotency hit ratio, webhook delivered/failed/dead, and outbox lag (pending age).** *(Proof: a metrics-render test asserting the recorded series.)*

### Python (the day-job axis)
- [ ] **pyright strict passes clean** — every `# type: ignore` / `# pyright: ignore`
  carries a comment saying what claim it makes. *(Proof: `make types` is green; each
  ignore reads as a decision.)*
- [ ] **No blocking call on the event loop** — the server runs clean under
  `PYTHONASYNCIODEBUG=1`, which logs any callback holding the loop past 100 ms. Every
  in-flight transfer on a process shares **one** thread, so a `time.sleep` in a retry
  backoff, the sync `redis.Redis` client instead of `redis.asyncio`, or a `requests`
  call in the dispatcher stalls *every* transfer at once — and quietly turns a p99
  target into a p50 one. Any sync work runs in a thread on purpose. *(Proof: a
  boss-fight run under the debug flag with no slow-callback warnings, or each one
  explained.)*
- [ ] **Bounded pools sized on purpose, together** — `DB_MAX_CONNECTIONS` × server
  processes stays under Postgres `max_connections` with room for the dispatcher, and
  is sized knowing a transfer holds its connection through every serialization retry;
  `REDIS_MAX_CONNECTIONS` and the dispatcher's delivery concurrency are chosen, not
  defaulted. *(Proof: `docs/18-design.md` names each number and the arithmetic behind
  it.)*
- [ ] **Graceful shutdown drains via the lifespan** — SIGTERM stops the dispatcher
  claiming, its current round settles within the drain budget, and the pool closes
  last; a transfer cut off mid-flight rolls back whole. *(Proof: a `docker stop -t 30`
  during a transfer storm that reaches `shutdown complete`, followed by `make audit`
  showing money conserved and no outbox event lost.)*
- [ ] **The container boots under uvloop** — `docker build` produces a runnable image,
  `/healthz` answers inside it, and `docker stop` reaches `shutdown complete` inside
  the grace period. The only check that runs asyncpg, redis-py and httpx on uvloop and
  exercises PID-1 signals; `make verify` sees neither. *(Proof: the build and the stop,
  noted in `docs/18-design.md`.)*
- [ ] **Profile committed** — a `py-spy` flamegraph and a `memray` run in
  `docs/18-benchmarks.md`, naming the top bottleneck. The interesting question is the
  split per transfer: how much is waiting on Postgres (and on serialization retries),
  and how much is Python — pydantic validation, JSON encoding, the idempotency hop.
  *(Proof: the flamegraph, and one sentence on what it changed.)*

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load test lives in `bench/`, the
   numbers in `docs/18-benchmarks.md`.
3. `docs/18-design.md` records the four decisions the SPEC grades: **sign convention
   (V1), isolation strategy (V2), idempotency TTL + ordering (V3), and why the outbox
   over fire-after-commit (V4)** — plus the webhook signature scheme.
4. `make verify` is green — `ruff format --check` → `ruff check` → `pyright` (strict) →
   `pytest` — and no `raise NotImplementedError` remains on a checked path.
5. The **profile** is committed alongside the numbers: a `py-spy` flamegraph and a
   `memray` run in `docs/18-benchmarks.md`, naming the top bottleneck. Numbers alone do
   not close this — you have to know *why* they are what they are. Where CPython can't
   reach a boss-fight target, **the gap and its cause are the finding** (GIL contention
   across in-flight transfers? GC pauses? a blocking call on the loop? pool starvation
   while transfers sit in retry?), recorded rather than designed around.

## 🐉 Boss fight — The Double Spend

> Two customers hit **pay** at the same instant from the same wallet. Both requests
> read the balance, both see enough, both approve. Under `READ COMMITTED` you have
> just minted money from nothing — the account is negative and your books no longer
> balance. This boss is a **storm of concurrent transfers fighting over one hot
> account**, laced with client retries that resubmit the *same* payment. It wins the
> moment a single cent is created, destroyed, or double-charged.

**Arena:** `bench/` load test (`oha` or `k6`) against the **production container**
(uvicorn on uvloop, `make verify`-clean — if it takes more than one server process,
the process count is part of the result) with Postgres + Redis up. Seed accounts with
a known total, run a mixed transfer workload plus a **single-hot-account contention**
scenario, and replay a fraction of requests with their original `Idempotency-Key`.
Snapshot the sum of balances before and after (`make audit`).

**The boss falls when ALL true:**
- [ ] ≥ **2,000 transfers/sec** sustained for 60s on the mixed multi-account workload.
- [ ] Under **1,000 concurrent transfers** racing on **one** no-overdraft account, the
  balance **never goes negative** — zero overdrafts, proven from the ledger, not vibes.
- [ ] **Money is conserved exactly:** `Σ` balances after == `Σ` before, to the cent,
  across the whole run (thousands of transfers).
- [ ] **Idempotency holds under retry:** every request replayed once with its key adds
  **zero** extra postings — posted-transaction count == unique-key count.
- [ ] **p99 ≤ 25ms** on the transfer path during the run, and serialization retries
  stay bounded (**< 5%** of requests need more than 3 retries).
- [ ] Every settled transfer delivers **exactly one** webhook to a healthy receiver
  (0 lost); only the deliberately-down endpoint reaches the **DLQ** after its cap.

**Proof:** methodology + before/after numbers (the conservation sum front and center)
in `docs/18-benchmarks.md` (hardware noted, commands reproducible via `bench/`).

## Suggested order of attack
1. Get the boring path working: `POST /accounts`, then a transfer that posts a
   balanced two-entry transaction straight to Postgres (no isolation, no idempotency).
2. Make the balance a **derived** read and lock down the balanced-post invariant (V1) —
   `draft_transfer` is pure, so property-test it with hypothesis first.
3. Attack it with concurrency; add `SERIALIZABLE` + retry and the no-overdraft
   enforcement until money is conserved under a storm (V2).
4. Add idempotency so a client retry can't double-post (V3).
5. Add the transactional outbox + signed webhook delivery with backoff/DLQ (V4) —
   `make receiver` (and `FAIL=1`) plus `make dispatcher` let you watch it.
6. Add auth + amount/currency validation, then benchmark, profile, document, and tune.

## Run it
```bash
make setup                  # copy .env.example → .env (then set WEBHOOK_SIGNING_SECRET)
make sync                   # uv: install the workspace venv from uv.lock
make dev                    # postgres + redis up → migrate → run the API
make account NAME=alice     # POST /accounts — 501 names the V1 todo
make transfer FROM=… TO=…   # POST /transfers — 501 names the V2 todo (KEY=… for V3)
make audit                  # Σ entries across the ledger — must always be 0
make receiver               # a webhook sink on :9000; `make dispatcher` to deliver (V4)
make verify                 # fmt-check → lint → types → test (what CI runs)
```
