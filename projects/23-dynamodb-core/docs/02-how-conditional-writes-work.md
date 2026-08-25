# How Conditional Writes Work — From First Principles

> Why "read, modify, write" is a bug in every concurrent system, what a
> compare-and-set actually buys you, and how three primitives — conditions, atomic
> updates, and transactions — cover the whole space between "one item" and "several
> items at once".
> No prior knowledge of concurrency control, locking, or transactions assumed.
>
> Prepares you for **V3** in [SPEC.md](../SPEC.md). Anchored to
> [conditions.py](../src/dynamodb_core/conditions.py),
> [errors.py](../src/dynamodb_core/errors.py) and the write path in
> [routes.py](../src/dynamodb_core/routes.py).
>
> The SPEC's *Suggested order of attack* puts this **before** V2 (indexes), because
> index maintenance has to respect conditional writes — you can't retrofit that.

---

## 0. The one sentence to hold onto

**A blind write cannot be made safe by being careful; it has to be made
conditional.**

No amount of reading-first, checking-first, or being-quick-about-it closes the
window between your read and your write. The only fix is to hand the *condition
itself* to the server and let it check and write in one indivisible step.

---

## 1. The problem: a lost update, in slow motion

Two support agents both apply a discount to order #7. Here is what the store sees:

```
   time    Agent A                          Agent B                  stored total
   ────────────────────────────────────────────────────────────────────────────
    t0     GetItem(order#7) -> total=100                                 100
    t1                                      GetItem(order#7) -> 100      100
    t2     computes 100 - 10 = 90                                        100
    t3                                      computes 100 - 20 = 80       100
    t4     PutItem(total=90)                                              90
    t5                                      PutItem(total=80)             80
   ────────────────────────────────────────────────────────────────────────────
   expected: 100 - 10 - 20 = 70          actual: 80        A's discount vanished
```

Now the part that makes this genuinely dangerous: **both agents got a `200 OK`.**
Nobody saw an error. There is no log line. The only evidence is a total that is
wrong by exactly one discount, discovered weeks later by finance.

The scaffold's module docstring puts it in one sentence:

> Two callers read an item, both modify it, both write it back. One update is gone
> and **nobody got an error**.

### Why the obvious fixes don't work

| "Fix" | Why it fails |
| --- | --- |
| Read again right before writing | The window shrinks; it doesn't close. B can still land between your re-read and your write. |
| Make the window tiny (fast code, same datacentre) | You've made the bug *rarer*, which means it now happens under load and never in your tests. |
| Have clients coordinate | They can't. They're different processes, possibly on different machines, that don't know about each other. |
| Wrap it in a mutex | Whose mutex? A lock in Agent A's process is invisible to Agent B's. A *shared* lock means a lock service — a new distributed system with its own failure modes (see §5). |
| Last-write-wins is fine, actually | Sometimes true! For a "last seen at" timestamp, sure. For money, no. The point is that it must be a *decision*, not a default you didn't notice. |

The window can't be closed from the client side. It has to be closed where the data
is.

---

## 2. Compare-and-set: move the check to where the data is

The fix is to stop saying *"write 90"* and start saying **"write 90, but only if
it's still 100."** The server checks and writes atomically — no gap for anyone to
interleave into.

```
   time    Agent A                                Agent B                stored
   ─────────────────────────────────────────────────────────────────────────────
    t0     GetItem -> total=100                                            100
    t1                                            GetItem -> total=100     100
    t4     Put(total=90) if total = 100  ✅                                 90
    t5                                            Put(total=80) if total = 100
                                                  ❌ ConditionalCheckFailed  90
   ─────────────────────────────────────────────────────────────────────────────
   B is TOLD. It re-reads (90), re-decides (70), retries. Nothing is lost.
```

That's it. That's the whole primitive. It has a name — **compare-and-set** — and
it's the same idea as the CPU instruction of the same name, scaled up to an item.

Two things to notice, because they're the design:

1. **The check happens at write time, inside the write.** The scaffold is explicit:
   *"Evaluated against the item as it exists at write time, atomically with the
   write itself. Evaluate it a moment earlier and you have reintroduced the race
   you were trying to close."* Where you call `evaluate_condition` from is not a
   detail — it's the correctness argument.

2. **The loser gets a distinct, non-retryable error.** Look at
   [errors.py](../src/dynamodb_core/errors.py):

```python
class ConditionalCheckFailed(AppError):
    """A ConditionExpression evaluated false (V3).

    Deliberately **not** retryable: the write was correctly refused, and the caller
    must re-read and decide again.
    """
    status_code = 409
    error_code = "ConditionalCheckFailedException"
```

`retryable = False` is a real statement, not metadata. Compare it with
`ProvisionedThroughputExceeded`, which sets `retryable = True` and sends a
`retry-after` header. **Retrying a throttle is correct; retrying a failed condition
blindly is a livelock** — the condition will keep failing until the client re-reads
and forms a *new* decision. Conflating those two errors is how you build a client
that hammers a server forever.

### The two idioms you'll implement

| Idiom | Expression | What it guarantees |
| --- | --- | --- |
| **Create-if-absent** | `attribute_not_exists(pk)` | `PutItem` succeeds exactly once. The second caller is told. This is how you make idempotent creation, claim a unique username, or elect a leader. |
| **Optimistic version check** | `version = :v` (and `SET version = :v+1`) | Under N concurrent updates, exactly one wins *per version*. Losers are told, re-read, retry. |

Both are V3 criteria. Both are two lines of expression text and zero locks.

---

## 3. Atomic updates: don't read the value at all

Conditions solve "don't clobber someone else." They're a clumsy answer to a
different question: **"add 1 to this counter."**

With compare-and-set you'd write a retry loop — read, increment, write-if-unchanged,
retry on failure. Correct, but under contention it's a spin: with N writers on one
counter, most attempts fail and retry, and throughput collapses exactly when you
need it.

The better answer is to never move the value to the client at all:

```
   read-modify-write                     server-side ADD
   ─────────────────                     ───────────────
   client: GET  hits -> 41               client: UpdateItem ADD hits :one
   client: computes 42                   server: reads 41, writes 42, atomically
   client: PUT  hits = 42                        (the value never left the server)
   ↑ two round trips, a window,          ↑ one round trip, no window,
     and a retry loop under contention     no retry loop
```

The scaffold names it:

> `ADD` is the one that makes an atomic counter work: the read and the write both
> happen here, server-side, so no two callers can interleave.

Your V3 criterion is the observable version: *"An atomic counter incremented by N
concurrent writers lands on exactly N."* Not N-3. Not "usually N."

[`apply_update`](../src/dynamodb_core/conditions.py) is where this lives, and the
clause list in its TODO is worth reading closely — `SET` with `if_not_exists` and
`list_append`, `REMOVE` on nested paths, `ADD` for numbers *and* set union,
`DELETE` for set difference. Two traps flagged there:

- **Use `Decimal` for the arithmetic.** A counter on `float` silently corrupts past
  2⁵³ — verified in [doc 01](01-how-order-preserving-key-encoding-works.md):
  `2**53 + 1` round-trips as `2**53`. Your counter would stop counting and never
  say so.
- **An update to a non-existent item creates it.** This surprises people. Pin it in
  a test either way, so the behaviour is a decision rather than an accident.

---

## 4. The expression language (and why it's split into three parts)

A `ConditionExpression` doesn't arrive as a string. It arrives as *three* things:

```python
@dataclass(slots=True)
class ConditionExpression:
    expression: str
    names: dict[str, str] = ...      # "#s" -> "status"
    values: dict[str, AttributeValue] = ...   # ":v" -> {"N": "3"}
```

So `"#s = :v"` plus `{"#s": "status"}` plus `{":v": {"S": "SHIPPED"}}`.

Why not just `"status = 'SHIPPED'"`? Two reasons, and the second is the one to
internalise:

1. **Reserved words.** `status`, `size`, `name` and a few hundred others are
   keywords in the expression grammar. `#s` lets you reference an attribute whose
   name collides with one.

2. **Values can never be parsed as expression text.** A value arrives through a
   separate channel and is only ever resolved *at evaluation time*, by dictionary
   lookup. There is no code path where a user-supplied value becomes part of the
   parsed expression.

That second point is **exactly the parameterised-query defence** from SQL. In
`WHERE name = ?` with the value bound separately, `'; DROP TABLE users; --` is a
name — a five-word string that happens to look scary. Splice it into the SQL text
and it's a statement. Same structure here. The scaffold asks you to name it as such
in the design doc, and the TODO says how to keep the property:

> resolve `#n` names and `:v` values **ONLY** at evaluation — never by string
> substitution into the expression, which is how you would reinvent injection.

If you ever find yourself building the expression string by concatenation, you have
written an injection vulnerability, in a system that had structurally prevented one.

### The grammar

The scaffold gives you the whole grammar in the TODO — operands, functions,
comparisons, boolean combination, parentheses. It's small: hand-writing a
recursive-descent parser for it is an afternoon, and the SPEC says the afternoon
*is* the value. Two pieces of advice embedded there worth pulling out:

- **Tokenize first, and keep the parser separate from the evaluator.** Parsing
  produces a tree; evaluating walks it against an item. Fusing them makes both
  untestable.
- **Absent is not false.** A path like `a.b[0].c` where `b` doesn't exist yields an
  *absent* operand. `absent = :v` is false, but `attribute_not_exists(a.b[0].c)` is
  **true**. Collapsing "absent" into "false" breaks `attribute_not_exists`, which
  is half of why conditions exist.

---

## 5. Transactions: when one item isn't enough

Conditions protect one item. Some invariants span several:

- Transfer £50 from account A to account B — both legs, or neither.
- Claim username `alice` *and* create the user record — never one without the other.
- Decrement stock *and* create the order — never an order for stock you don't have.

A partial application here is worse than a failure. "The money left A and never
arrived at B" is a real incident with a real number attached.

[`TransactItem`](../src/dynamodb_core/conditions.py) models one leg:

```python
class TransactOperation(StrEnum):
    PUT = "Put"
    UPDATE = "Update"
    DELETE = "Delete"
    CONDITION_CHECK = "ConditionCheck"
```

`CONDITION_CHECK` is the interesting one, and the scaffold explains why:

> Not a write at all: asserts a condition on an item this transaction reads but
> does not modify. Without it you cannot make a decision about item A depend on the
> state of item B.

"Create this order **only if** the customer's account is active" — the account
isn't being written, but the decision depends on it. Without a condition check
you'd have to read the account first, and you'd be back to a read-then-write race,
one level up.

### Optimistic vs. pessimistic — the choice the SPEC grades

Your Definition-of-done requires `docs/23-design.md` to name your transaction
protocol. The two families:

| | **Pessimistic** (lock first) | **Optimistic** (detect conflict) |
| --- | --- | --- |
| How | Acquire a lock on every item, then read/write, then release | Do the work provisionally; check nothing changed; commit or abort |
| Best when | Contention is high — conflicts are common, so retrying is wasteful | Contention is low — most transactions don't conflict, so checking is cheaper than locking |
| Failure mode | **Deadlock** (A holds x wants y; B holds y wants x) and a crashed client holding a lock forever | **Livelock** / starvation — a transaction that keeps losing and never completes |
| Deadlock avoidance | Order acquisitions consistently, or detect cycles and abort a victim | Not applicable — nothing is held |
| Cost of a conflict | Waiting | Redoing the work |

Your criterion — *"Conflicting concurrent transactions do not deadlock: one aborts
with a distinct error"* — is satisfiable from either family, and that's deliberate:
the SPEC grades the *decision and its defence*, not a particular answer. What it
will not accept is a system that hangs.

The one piece of pre-work the scaffold does hand you is
[`check_transaction`](../src/dynamodb_core/conditions.py):

> DynamoDB rejects a transaction that touches the same item twice, because the
> result would depend on an ordering it never promised.

Cheap to implement, and it removes an entire category of "why did only one of my
two updates land" confusion. Note the reasoning: the API never promised an order
for the legs, so a transaction whose result depends on one is asking for a
guarantee that doesn't exist. Rejecting it is more honest than picking an order.

---

## 6. The write path: ordering is the design

Here's where V3 stops being a module and becomes the spine of the project. The
`PutItem` branch in [routes.py](../src/dynamodb_core/routes.py) carries this TODO:

```python
            # TODO(V3 -> V2 -> V5): the real write path is a sequence, and its
            # ORDER is the design: evaluate the condition, apply the write, update
            # every index, append the stream record — atomically, so a reader
            # never sees a half-applied write and a crash never splits them.
```

Four steps, and every adjacent pair has a failure mode if you get it wrong:

```
   ┌─ 1. evaluate condition ──────────────────────────────────┐
   │      too early?  the race is back (§2)                    │
   ├─ 2. apply the write ─────────────────────────────────────┤
   │      before the condition?  a failed condition mutated    │
   │      state — your criterion says it must change NOTHING   │
   ├─ 3. maintain every index (V2) ───────────────────────────┤
   │      skipped when the condition failed?  correct.         │
   │      skipped when it passed?  orphaned index entries      │
   ├─ 4. append the stream record (V5) ───────────────────────┤
   │      before the write commits?  a record for a write      │
   │      that never happened — the V5 crash test hunts this   │
   └───────────────────────────────────────────────────────────┘
```

And a criterion that catches people: *"a failed condition **changes nothing** while
still costing capacity (both observable)."* The work of checking was real work, so
it's billed — but the item is untouched. Those two facts have to be true
simultaneously, which means capacity metering (V4) can't simply live at the end of
a successful write path.

This is exactly why the SPEC sequences V3 before V2. Build indexes first and
you'll wire index maintenance into an unconditional write path, then have to
re-thread conditions through it afterwards.

---

## 7. Mental-model summary

| The instinct | The correction |
| --- | --- |
| "I'll read, then write. It's two calls but it's fine." | It's a lost update waiting for concurrency. The window can't be closed from the client. |
| "A lost update will show up in the logs." | It cannot. Both writers got `200 OK`. That's what makes it dangerous. |
| "I'll re-read just before writing to be safe." | Smaller window, same bug — now it only fires under production load. |
| "Conditional check failed — I'll retry." | Not retryable. Re-read, re-decide, *then* retry. Blind retry is a livelock. |
| "Throttles and condition failures are both 'try again'." | Opposites. One is backpressure (`429`, retryable, `retry-after`); one is a correct refusal (`409`, non-retryable). |
| "An atomic counter is a read-modify-write with a condition." | It's a server-side `ADD`. The value never leaves the server, so there's no window and no retry storm. |
| "I'll build the expression string from the values." | That's injection. Names and values resolve at evaluation, by lookup, never by substitution. |
| "A missing attribute is falsy." | Absent ≠ false. `attribute_not_exists` depends on the difference. |
| "Transactions mean locks." | Optimistic protocols use no locks at all. Both are valid; the SPEC grades which you chose and why. |
| "A partly-applied transaction is a rare edge case." | It's the one thing a transaction exists to prevent. A reader must never observe it. |

---

## 8. Where you'll build this

**Module:** [conditions.py](../src/dynamodb_core/conditions.py):

| `todo` | What it owes you |
| --- | --- |
| `evaluate_condition` | Tokenizer → recursive-descent parser → evaluator. Nested paths, absent-vs-false, names and values resolved at evaluation only. |
| `apply_update` | `SET` / `REMOVE` / `ADD` / `DELETE`, nested paths, `Decimal` arithmetic, create-on-update. |
| `check_transaction` | Reject duplicate `(table, key)` legs and over-large transactions, before anything applies. |

Plus two branches in [routes.py](../src/dynamodb_core/routes.py):
`UpdateItem` (`"V3: UpdateItem — condition + update expression"`) and
`TransactWriteItems` (`"V3: TransactWriteItems — all-or-nothing across items"`),
and the ordering of the `PutItem` write path from §6.

**Done-when criteria this doc unlocks** (from [SPEC.md](../SPEC.md) V3):
`attribute_not_exists` making a `Put` succeed exactly once with a distinct
non-retryable error for the loser; exactly one winner per version under N
concurrent updates; an atomic counter landing on exactly N; `SET`/`REMOVE`/`ADD` on
nested paths with a failed condition changing nothing but still costing capacity;
all-or-nothing transactions never observable mid-flight; no deadlock under
conflict.

**A note on the Proof line.** It asks for *"concurrency tests driving real parallel
writers (not sequential calls)"*. Sequential calls will pass a broken
implementation every time — the race needs actual contention to appear. Budget for
that; it's the hardest part of the testing here, and the horizontal checklist's
*"the GIL's cost is measured, not assumed"* item is watching the same code.

**Stuck?** `/hint` for graduated nudges; `/quest` to run the vertical with
acceptance tests written up front.

**Next:** [03-how-secondary-indexes-work.md](03-how-secondary-indexes-work.md) —
now that writes are conditional, index maintenance has something correct to hang
off.
