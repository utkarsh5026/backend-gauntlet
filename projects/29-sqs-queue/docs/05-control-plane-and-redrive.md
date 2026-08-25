# The Control Plane — Idempotent Creation, Attributes as a Contract, and the Way Back

> Teaches why `CreateQueue` being idempotent is what makes declarative infrastructure
> possible, why "does this attribute affect existing messages?" is a question with no
> default answer, and what a dead-letter queue is actually for. No prior knowledge
> assumed — not of Terraform, not of SQS redrive.
>
> Prepares you for **V6** in [`src/sqs_queue/control.py`](../src/sqs_queue/control.py)
> (`normalize_attributes`, `creation_conflicts`, `application_table`, `apply_attributes`,
> `redrive_check`, `move_to_dlq`, `redrive_back`). The store plumbing it sits on is
> already done in [`state.py`](../src/sqs_queue/state.py).

---

## The one sentence to hold onto

**A control plane's job is to make "make it so" safe to say a thousand times — which
means creation must be idempotent, every attribute must be validated before it can hurt
you, and every message must have a way out.**

---

## 1. Control plane vs. data plane

Two surfaces, two completely different jobs, and it is worth naming the split before
anything else:

| | Control plane | Data plane |
|---|---|---|
| Actions | `CreateQueue`, `SetQueueAttributes`, `DeleteQueue` | `SendMessage`, `ReceiveMessage`, `DeleteMessage` |
| Call rate | a few per deploy | tens of thousands per second |
| Who calls it | Terraform, a CI job, an operator | your application, constantly |
| What it costs to be slow | nothing | everything |
| What it costs to be wrong | an outage that outlives the deploy | one message |

That asymmetry is why real IAM, real SQS and this project keep them separate — and it is
why the expensive validation belongs on the left-hand column. **Anything you can check at
`SetQueueAttributes` time you must not check at delivery time**, because on the hot path
the only safe answer left is to refuse a message the client already believes it sent.

---

## 2. Idempotent creation, and why it's the whole ballgame

Here is a deploy script:

```
CreateQueue("orders")
```

Run it twice. What should happen?

The obvious answer is "an error, the queue exists". And that answer makes the script
unrunnable, because now every caller needs:

```
if not queue_exists("orders"):     # ← and here is a race
    CreateQueue("orders")
```

Two CI jobs, two deploys, one retry after a timeout — and the check-then-create window is
open. This is the same shape as the check-then-park race in
[`02-long-polling.md`](./02-long-polling.md) §4 and the check-then-claim race in
[`04-deduplication-windows.md`](./04-deduplication-windows.md) §5. Same bug, third
appearance.

**The fix is to make the *server* idempotent instead:**

```
CreateQueue("orders", attrs)  where "orders" exists with the SAME attributes  -> 200, same URL
CreateQueue("orders", attrs)  where "orders" exists with DIFFERENT attributes -> conflict
```

Now the script has no branch and no race. Run it a thousand times; the thousandth is a
no-op. That single property is what makes declarative infrastructure — Terraform,
Kubernetes, CloudFormation — possible at all: they *all* work by saying "make this exist"
repeatedly and relying on the server to make repetition free.

**And the interesting part is the comparison**, which is where `creation_conflicts` in
[`control.py`](../src/sqs_queue/control.py) lives. Two questions with no default answer:

| Question | If you say... | Consequence |
|---|---|---|
| Which attributes participate in identity? | *all of them* | someone tweaking a visibility timeout by hand breaks the next deploy |
| | *only the ones sent* | two different configs can both "match" |
| What does an omitted attribute mean? | *matches the default* | strict; a deploy that stops sending a field now conflicts |
| | *matches anything* | permissive; drift goes unnoticed |

Get either wrong and you've built an API that works on the first deploy and fails on the
second — the worst possible failure schedule, because it passes every test you wrote.

`QueueNameExists` in [`errors.py`](../src/sqs_queue/errors.py) is *only* for the genuine
conflict. The identical-attributes case is not an error; it's the feature.

> Related and already implemented for you: `QueueStore.delete` starts a 60-second cooldown
> on the name (`QUEUE_NAME_COOLDOWN_SECONDS` in [`state.py`](../src/sqs_queue/state.py)).
> That looks like bureaucracy and it's a correctness property — an in-flight request
> holding the old queue's URL must not land in a *new* queue that reused the name.

---

## 3. The question nobody documents: does this affect existing messages?

You lower a queue's visibility timeout from 30s to 5s. There are 400 messages in flight,
each with a lease granted under the old value.

What happens to them?

Both answers are defensible:

| | Leases shorten immediately | Existing leases stand |
|---|---|---|
| Argument | the setting is the truth; act on it | consumers were promised 30s and are relying on it |
| Consequence | 400 messages may be redelivered while consumers are still working on them | the change silently doesn't apply for up to 30 seconds |

Neither is wrong. What *is* wrong is not deciding — because then the behaviour is whatever
your loop happened to do, it's obvious to you and unknowable to everyone else, and it will
be discovered during an incident.

Same question, several times over:

| Attribute changed | The question |
|---|---|
| `VisibilityTimeout` ↓ | do in-flight leases shorten? |
| `MessageRetentionPeriod` ↓ below the age of existing messages | do they vanish immediately? |
| `DelaySeconds` | do already-delayed messages re-time? |
| `RedrivePolicy` `maxReceiveCount` ↓ | do messages already over the new limit go straight to the DLQ? |
| `MaximumMessageSize` ↓ | are already-stored larger messages affected? (should be no — but say so) |

This is why `AttributeApplication` in [`control.py`](../src/sqs_queue/control.py) is a
**modelled type returned by the code**, not a markdown table. A table that lives only in a
doc drifts from the implementation within a month; one the code produces can be asserted
against in a test. That's the graded artifact: the table in `docs/29-design.md`, and an
implementation that matches it.

---

## 4. Validation belongs at the boundary

`normalize_attributes` is where every ceiling in [`config.py`](../src/sqs_queue/config.py)
gets enforced. Note the deliberate split there between `default_*` (what a **new queue**
gets) and `max_*` (what `SetQueueAttributes` may **set**) — collapsing them into one
number is how a caller ends up able to set a 30-day visibility timeout because somebody
wanted a friendlier default.

The real SQS numbers, for reference:

| Attribute | Range |
|---|---|
| `VisibilityTimeout` | 0 – 12 hours |
| `MessageRetentionPeriod` | 60 s – 14 days (default 4 days) |
| `DelaySeconds` | 0 – 15 minutes |
| `MaximumMessageSize` | 1 KB – 256 KB |
| `ReceiveMessageWaitTimeSeconds` | 0 – 20 s |
| In-flight per queue | 120,000 standard / 20,000 FIFO |

Two rules that are easy to get wrong:

**Refuse unknown attribute names.** Ignoring one is how a team spends a week wondering why
their setting has no effect. `InvalidAttributeName` exists for this.

**Refuse FIFO-only attributes on standard queues.** Accepting `ContentBasedDeduplication`
on a standard queue and silently ignoring it is the same failure with a security flavour —
somebody believes they have deduplication and does not.

---

## 5. Redrive: what the receive count was for

`Message.receive_count` has been quietly incrementing since [doc 00](./00-receipt-handles-and-leases.md). Here is what it buys.

A message fails. Its lease expires, it comes back, another consumer takes it, it fails
again. Left alone this is an infinite loop: one bad message consuming consumer capacity
forever, and on a FIFO queue ([doc 03 §6](./03-fifo-ordering-keys.md)) stalling its entire
group permanently.

The redrive policy ends it:

```
   maxReceiveCount = 3

   delivery 1 -> fails
   delivery 2 -> fails
   delivery 3 -> fails
   delivery 4 -> ...does this happen?     ← the off-by-one, and it's yours to pin
                 the message moves to the dead-letter queue instead
```

That off-by-one is worth a test rather than a guess. With `maxReceiveCount = 3`, does the
consumer see the message three times or four? Both readings are defensible from the name;
only one is what you implemented. `redrive_check` runs on the receive path **before** the
message is handed out, precisely so a message that has already hit its limit is not
delivered one final time on its way out.

**Now notice what SQS does *not* have here.** No retry schedule. No exponential backoff.
No jitter. A message that fails comes straight back after its visibility timeout, at the
same interval, every time.

Project 04 made the opposite choice — backoff with jitter, in the broker (see
[04's V3 doc](../../04-job-queue/docs/03-retries-backoff-dlq.md)). Which is right?

| Backoff in the broker (04) | Backoff in the consumer (SQS) |
|---|---|
| Every consumer gets it for free | Every consumer must implement it |
| The broker must model "failure" — so `nack` becomes an API | The broker only knows "delivered" and "deleted" — a smaller surface |
| One retry policy for everyone | Each consumer tunes its own, via `ChangeMessageVisibility` |

The SQS answer is that a consumer expresses backoff by *extending its own visibility* —
which keeps the broker ignorant of what failure even means. That is a real architectural
position, and the SPEC asks you to say which you agree with and why. There isn't a right
answer; there's a documented one.

---

## 6. The way back out

A dead-letter queue you can put messages into and not get them out of is a data-loss bug
with a friendly name.

Think about the operator's actual day: 4,000 messages landed in `orders-dlq` overnight
because a downstream service was down. That service is fixed now. Those messages are all
perfectly good. They need to go back.

Two things `redrive_back` must get right, and they pull in opposite directions:

**Reset the receive count.** A message redriven with `receive_count` still at 3 goes
straight back to the DLQ on its first delivery. You've built a very expensive no-op.

**Do not reset the message id.** Consumers are using it as an idempotency key. Change it
and every consumer's dedup logic sees a brand-new message — the exact duplicate-processing
outcome the whole system is trying to avoid.

And `move_to_dlq` has an obligation nobody thinks about until they're triaging: **preserve
provenance.** The original queue, the original send time, the receive count that got it
here. A DLQ full of messages with fresh timestamps and no history is a DLQ nobody can
triage — the operator can't tell a message that failed once at 3am from one that has been
poison since last Tuesday.

> Historical note worth its own sentence: real SQS shipped dead-letter queues years before
> it shipped a redrive API. For a long time, the way out was a script you wrote yourself.
> That gap tells you how the omission gets discovered — after you need it.

---

## 7. Mental model summary

| Question | Answer |
|---|---|
| What makes declarative infra possible? | Idempotent creation |
| What's the hard part of idempotent creation? | Deciding which attributes participate, and what "omitted" means |
| Where does validation belong? | The control plane, at write time — never at delivery time |
| What must the design doc contain? | The per-attribute applies-to-existing table, matching the code |
| What is `receive_count` for? | Redrive: the poison-message exit |
| Does SQS back off retries? | No — the consumer does, via `ChangeMessageVisibility` |
| What must a redrive reset? | The receive count. And **not** the message id |
| What must a DLQ preserve? | Provenance — or nobody can triage it |
| Why the 60s name cooldown? | So a stale URL can't resolve into a new queue |

---

## Where you'll build this

[`src/sqs_queue/control.py`](../src/sqs_queue/control.py) — seven
`raise NotImplementedError`s. Call sites are marked in
[`routes.py`](../src/sqs_queue/routes.py): `_create_queue`, `_get_queue_attributes`,
`_set_queue_attributes`, `_delete_queue`, `_purge_queue`, `_start_message_move_task`, and
the redrive check inside `_receive_message`.

The store plumbing underneath — create, lookup by name and URL, the deletion cooldown, the
`O(1)` counters — is already implemented in [`state.py`](../src/sqs_queue/state.py) and
tested in [`tests/test_smoke.py`](../tests/test_smoke.py).

Done-when criteria this unlocks (V6 in [`SPEC.md`](../SPEC.md)): idempotent create and its
conflict case, bounded validated attributes, the documented applies-to-existing decisions,
the `maxReceiveCount` → DLQ path, an inspectable and redrivable DLQ, retention and oldest-
message age, immediate `DeleteQueue`, and standard-vs-FIFO attribute separation.

**A good first move:** `_purge_queue` has a question in its TODO that you can't answer
without having decided §3's rule — what happens to messages currently in flight, whose
handles are held by consumers about to delete them? Answer that one and the rest of the
table gets easier.

`/hint 29 V6` · `/quest 29 V6`
