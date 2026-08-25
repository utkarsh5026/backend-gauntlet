# Receipt Handles — Why a Queue Can't Let You Delete by ID

> Teaches why "delete this message" is the wrong API, what a receipt handle actually
> is, and the generation trick that makes a stale delete decidable instead of a coin
> flip. No prior knowledge assumed — not of SQS, not of distributed leases.
>
> Prepares you for **V1** in [`src/sqs_queue/inflight.py`](../src/sqs_queue/inflight.py)
> (`ReceiptHandleCodec.mint`, `.parse`, `InflightTable.receive/delete/change_visibility`).
> The types it talks about live in [`src/sqs_queue/models.py`](../src/sqs_queue/models.py).

---

## The one sentence to hold onto

**A consumer never owns a message — it owns a *delivery*, for a while — so every
operation it performs must name the delivery it was given, not the message.**

If you internalize only that, the rest of this doc is just *why* the distinction is
invisible until it costs you, and *how* you make it checkable.

---

## 1. The problem before the solution

Project 04 already built the lease idea: a claim is time-boxed, and when the clock
runs out the job goes back in the pool. That was a queue running *inside your
process*, where the worker was your own code.

Now the worker is a stranger on the other side of a socket. You cannot see its stack,
you cannot call its functions, and — this is the part that matters — **you cannot tell
the difference between a worker that died and a worker that is merely slow.** Both look
identical from here: silence.

So you hand out a message, start a timer, and wait. Here is the trace that ruins the
obvious API:

```
 t=0    consumer A: ReceiveMessage        -> message M
                    starts a 40-second job
 t=30   (M's 30s visibility timeout expires; M becomes available again)
 t=31   consumer B: ReceiveMessage        -> message M          ← the *same* message
                    starts the same 40-second job
 t=40   consumer A: DeleteMessage(M)      -> deleted ✅
                    ...but it deleted B's delivery.
 t=71   consumer B: DeleteMessage(M)      -> deletes nothing; M is already gone
                    B's lease has nothing to release
```

Now count what went wrong:

| Symptom | Why |
|---|---|
| The job ran **twice** | A's lease expired while A was still working — that's at-least-once doing its job |
| A's delete **succeeded** | `DeleteMessage(message_id)` has no way to know A's delivery was over |
| B's work was **silently discarded** | The message it was working on vanished mid-flight |
| Nothing was **logged as wrong** | Both calls returned success. Both consumers behaved correctly |

That last row is what makes this dangerous. Every participant is honest, every response
is a 200, and the system is wrong. There is no error to grep for.

And it compounds. If B's delete had *also* been a no-op on a message that had been
re-delivered to C, you get a queue where messages come back forever and no consumer can
ever successfully finish one. At-least-once quietly becomes at-least-*forever*.

---

## 2. The diagnosis: two different nouns wearing one name

Look at what `DeleteMessage(message_id)` is actually asking for. It says:

> *"Remove the message with this id."*

But what the consumer means is:

> *"I finished the work I was handed at 12:00:03. Release **that**."*

Those are different sentences. The first names a thing that outlives every delivery; the
second names one episode in that thing's life. `message_id` cannot express the second,
because it is the same string on every delivery.

```
message M ──┬── delivery #1  (consumer A, t=0  .. t=30, expired)
            ├── delivery #2  (consumer B, t=31 .. t=61, expired)
            └── delivery #3  (consumer C, t=62 ..      , live)
              ▲
              └── "M" refers to all of these. A consumer needs to refer to exactly one.
```

**The fix is to give the consumer a name for the row it is actually holding.** That name
is the **receipt handle**: minted per delivery, handed out by `ReceiveMessage`, and
presented back on `DeleteMessage` and `ChangeMessageVisibility`.

With handles, the same trace becomes decidable:

```
 t=0    A receives M -> handle H1   (delivery #1)
 t=30   lease expires; M moves to delivery #2
 t=31   B receives M -> handle H2
 t=40   A: DeleteMessage(H1) -> H1 names delivery #1. Delivery #1 is over. REFUSED.
 t=71   B: DeleteMessage(H2) -> H2 names delivery #2, which is live. DELETED. ✅
```

Nothing about A changed. A is still slow, still honest, still confused. But its stale
delete can no longer reach B's work, because it is *carrying evidence of which delivery
it belongs to*.

---

## 3. The generation counter — making "stale" a comparison

You could track "who holds what" in a table of live handles. That works and it is
bookkeeping: you have to add on receive, remove on delete, remove on expiry, and every
path that forgets one is a leak or a bug.

There's a cheaper framing. Give the message a counter that increments **every time a
delivery begins or ends**, and put that number in the handle:

```
Message M:  generation = 0    state = available
   A receives    ->  generation = 1    state = inflight     handle H1 carries gen=1
   lease expires ->  generation = 2    state = available
   B receives    ->  generation = 3    state = inflight     handle H2 carries gen=3
```

Now "is this handle stale?" is one integer comparison:

| Handle presented | Carries | Message is at | Verdict |
|---|---|---|---|
| `H1` | gen 1 | gen 3 | stale — the delivery it names ended |
| `H2` | gen 3 | gen 3 | current — act on it |
| `H2` (again, after a successful delete) | gen 3 | deleted | already gone — see §5 |

That is the whole mechanism. `Message.generation` already exists in
[`models.py`](../src/sqs_queue/models.py) and `ParsedHandle.generation` in
[`inflight.py`](../src/sqs_queue/inflight.py) — the docstrings there say the same thing
from the code's side.

This pattern has a name in the literature: a **fencing token**. It shows up wherever one
party can be replaced while it still believes it is in charge:

| System | The token | What it fences off |
|---|---|---|
| SQS | receipt handle | a delete from an expired delivery |
| Kafka | producer epoch | writes from a zombie producer after a restart |
| etcd / Chubby | lease id + revision | a client acting on a lock it already lost |
| Optimistic locking in SQL | `version` column | an update computed from a stale read |

Same shape every time: **the new generation invalidates the old one's authority, without
the old one having to notice.**

---

## 4. The design space (this is the part you decide)

The SPEC's criterion is that a handle must be **unforgeable**: a client that has never
received a message must not be able to construct a valid handle for it. That leaves a
real choice, and the two ends of it trade off against each other.

**End A — the handle is a lookup key.** Mint a random opaque string, store it in a table
mapping handle → (message, generation), and check membership on presentation.

**End B — the handle carries its own contents.** Encode the facts into the token itself
and attach something that proves *you* produced it, so it can be checked without a
lookup.

Weigh them on the axes the SPEC actually grades:

| Axis | Why it matters here |
|---|---|
| Cost per receive | Minting is on the hot path — the boss fight's drain scenario runs it 10,000×/sec |
| Cost per delete | Same, in the other direction |
| Memory | A table of live handles is state proportional to in-flight messages (capped at 120,000/queue by `max_inflight_per_queue`) |
| What leaks | Everything inside a self-describing handle is visible to whoever holds it |
| Cross-queue safety | A handle for `orders` must not work against `payments` — how does each end enforce that? |
| Restart behaviour | After a process restart, which handles should still verify? Is that what you want? |

Notice the last row is not obviously "yes". A handle that survives a restart lets a
consumer delete a message that the new process has no record of delivering. A handle that
does not survive means every in-flight message comes back after a deploy. Both are
defensible. **Pick one deliberately** and write it in `docs/29-design.md` — that write-up
is a graded Proof.

> The one thing that is *not* a judgement call: the check must not be "does this string
> look right". `ReceiptHandleIsInvalid` in
> [`errors.py`](../src/sqs_queue/errors.py) exists for input you did not produce, and the
> forgery test in the SPEC is aimed straight at it.

---

## 5. Three outcomes, not two

`DeleteOutcome` in [`inflight.py`](../src/sqs_queue/inflight.py) has three members, and
the reason is worth sitting with, because the naive version has two.

| Outcome | What happened | What the consumer should do | What *you* should learn from it |
|---|---|---|---|
| `DELETED` | Handle named the live delivery | Nothing, it worked | — |
| `ALREADY_DELETED` | Same handle, delete called twice | Nothing, it worked | Retries are normal; this is not an error |
| `SUPERSEDED` | Valid handle, older delivery | Stop, re-receive | **Your visibility timeout is too short** |

Idempotency (`ALREADY_DELETED`) is not politeness. A queue client whose `DeleteMessage`
response was lost to a network blip *will* send it again, and if the second call is an
error, every consumer in the world needs retry logic around a call that already
succeeded.

And the third row is a diagnostic, not just a refusal. A queue that reports zero
superseded deletes has healthy timeouts. A queue reporting thousands per minute is
telling you that its consumers are routinely slower than the lease you gave them — which
you would never learn if stale deletes were silently swallowed. That is exactly why the
observability checklist in [`SPEC.md`](../SPEC.md) asks for stale and malformed handle
rejections to be counted **separately**.

---

## 6. Visibility changes: the same rule, pointed the other way

`ChangeMessageVisibility` is how a consumer says *"still working, give me longer"* — and
at `timeout=0`, *"take it back now"*, which is the closest thing this protocol has to a
nack.

It obeys exactly the same rule as delete: only the holder of the current handle may move
the lease. But it has a wrinkle worth seeing before you write it.

```
 t=0    C receives M, gen=3, lease until t=30, holds handle H3
 t=25   C: ChangeMessageVisibility(H3, 60)   -> lease now runs to t=85
```

There is now a timer registered for `t=30` that should not fire. Two questions fall out
of that, and both are graded:

1. **What happens if it fires anyway?** (Look at the generation. Has it changed? Should
   it have?) This is the hand-off between V1 and V2 — see
   [`01-deadline-engines.md`](./01-deadline-engines.md) §5.
2. **What if a consumer extends every 10 seconds for an hour?** That is 360 obsolete
   timers. Harmless individually; the SPEC's bounded-residue criterion is about the
   accumulation.

And a smaller decision hiding in `timeout=0`: the delivery is over, so what should happen
to `H3`? Answer that and the "immediately receivable again" criterion falls out.

---

## 7. The consequence you cannot engineer away

Receipt handles fix *misdirected* deletes. They do not fix duplicate *work*.

Consumer A still ran that job. It ran the whole thing — sent the email, charged the card,
posted the webhook — and then found out its delivery had ended. Nothing in this module
un-sends an email.

This is the at-least-once bill, and it is the same one project 04 pays (see
[04's V2 doc](../../04-job-queue/docs/02-leases-visibility-timeout.md)). The only real
defence is **idempotent handlers**: the consumer's work must be safe to perform twice.
Receipt handles narrow the damage to "the work happened twice", which is a problem the
consumer can solve. Without them, the damage is "the work happened twice *and* the queue
lost track of which copy is live", which nobody can solve.

Worth saying plainly, because it is the sentence people skip: **a queue cannot give you
exactly-once. It can only stop making things worse.**

---

## 8. Mental model summary

| Question | Answer |
|---|---|
| What does a consumer hold? | A delivery, not a message |
| What names a delivery? | A receipt handle, minted per receive |
| How do you tell a stale handle from a live one? | Compare generations — one integer |
| How do you tell a stale handle from a forged one? | You minted the first, you didn't mint the second |
| What does a stale delete do? | Nothing to the live delivery — and it increments a metric you care about |
| What does a double delete do? | Nothing, successfully |
| What is the general pattern called? | A fencing token |
| What does none of this fix? | Duplicate execution. Handlers must be idempotent |

---

## Where you'll build this

[`src/sqs_queue/inflight.py`](../src/sqs_queue/inflight.py) — six
`raise NotImplementedError`s:

- `ReceiptHandleCodec.mint` / `.parse` — the token itself, §4's decision made concrete
- `InflightTable.receive` — available → in-flight, generation bumped, handle minted
- `InflightTable.delete` — the three outcomes of §5
- `InflightTable.change_visibility` — §6
- `InflightTable.expire_visibility` — the lease running out, called by V2's engine

The Done-when criteria this doc unlocks, from [`SPEC.md`](../SPEC.md) V1: distinct
handles per delivery, superseded deletes leaving the live delivery alone, idempotent
delete, visibility changes gated on the current handle, unforgeability, invisibility
while in flight, and the receive count.

**Write the stale-delete test first.** Slow consumer A, expiry, consumer B, A deletes —
the trace from §1. It fails loudly against a naive implementation, and once it passes
you have the whole vertical.

Stuck on a specific step? `/hint 29 V1` for a graduated nudge, `/quest 29 V1` for a
guided build with acceptance tests up front.
