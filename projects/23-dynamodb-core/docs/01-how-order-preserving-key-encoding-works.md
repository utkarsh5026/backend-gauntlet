# How Order-Preserving Key Encoding Works — From First Principles

> Why turning a key value into bytes is the single hardest correctness problem in
> V1, why `"10"` sorting before `"9"` breaks every range query you will ever write,
> and what an encoding has to guarantee before you can trust `between`.
> No prior knowledge of byte ordering, floating point, or serialization assumed.
>
> Prepares you for the second half of **V1** in [SPEC.md](../SPEC.md). Anchored to
> `encode_key` in [table.py](../src/dynamodb_core/table.py) and `KEY_TYPES` in
> [item.py](../src/dynamodb_core/item.py).
>
> Read [00-how-partition-keys-decide-placement.md](00-how-partition-keys-decide-placement.md)
> first — it establishes *why* a partition is kept sorted. This doc is about making
> the sort actually correct.

---

## 0. The one sentence to hold onto

**A key encoding is a promise that comparing the bytes gives the same answer as
comparing the values — and if it lies even once, every range query is silently
wrong.**

Not slow. Not crashy. *Wrong*, returning plausible-looking results that quietly
omit rows. This is the kind of bug that survives to production because nothing
throws.

---

## 1. Why encode at all?

The scaffold states the contract in one line:

```python
def encode_key(value: AttributeValue) -> bytes:
    """Encode a key attribute into **order-preserving** bytes.

    The whole of V1's correctness rests here: `encode_key(a) < encode_key(b)` must
    hold exactly when `a` sorts before `b` in DynamoDB's ordering, for every pair
    of values of the same type.
    """
```

Two words matter: **exactly when**. That's an if-and-only-if, in both directions,
for *every* pair. Not "usually," not "for the values in my test file."

Why bytes at all, rather than comparing the typed values directly? Three reasons,
and they compound:

1. **Uniformity.** A partition holds items whose sort keys are all one type, but
   your storage code shouldn't branch on the type on every comparison. One
   `bytes` comparison works for `S`, `N` and `B` alike.
2. **Durability.** Keys get written to disk and to the WAL (the horizontal
   checklist requires one). Bytes are what disks store; an in-memory `Decimal` is
   not.
3. **Composition.** A composite key is a partition key *and* a sort key glued into
   one addressable thing. Gluing bytes is easy; gluing "a `Decimal` and a `str`" is
   not a thing.

So: values in, bytes out, order preserved. How hard can it be?

---

## 2. Strings are easy (and it's worth knowing why)

For `S`, UTF-8 already sorts correctly. That's not a happy accident — it's a
designed property of UTF-8: comparing UTF-8 byte sequences lexicographically gives
the same result as comparing the Unicode code points.

Verified:

```
     'apple' < 'banana'   codepoint=True   bytes=True   ✅
         'Z' < 'a'        codepoint=True   bytes=True   ✅
         'a' < 'ab'       codepoint=True   bytes=True   ✅
         'é' < 'z'        codepoint=False  bytes=False  ✅ (agree, both say no)
```

That last one is a good reminder that "correct" means *consistent with the
declared ordering*, not *what a human expects*. `é` (U+00E9) sorts after `z`
(U+007A) by code point. DynamoDB sorts strings by UTF-8 bytes, full stop — it does
not do locale-aware collation. If your users need "é next to e", that is an
application concern, and pretending otherwise is how you get an encoding that
disagrees with itself.

`B` (binary) is even easier: the bytes *are* the bytes.

Which leaves `N`.

---

## 3. Numbers: four encodings that look fine and aren't

Here's the true order we have to reproduce:

```
  -20, -1, 0, 2, 9, 10, 100, 1000
```

Now watch four reasonable-sounding encodings fail. Every line below is **real
output**, not a sketch.

### Attempt 1 — just encode the decimal string

```
  str().encode()  ->  -1, -20, 0, 10, 100, 1000, 2, 9
```

Broken twice over. `"10"` sorts before `"9"` because `'1' < '9'` and comparison
stops at the first differing byte — length never enters into it. And `-1` sorts
before `-20` for the same reason.

### Attempt 2 — zero-pad the digits to a fixed width

```
  -000001, -000020, 000000, 000002, 000009, 000010, 000100, 001000
```

Progress: the positives are now right, because equal width makes lexicographic
comparison agree with magnitude. And the negatives at least clump at the front,
since `-` is `0x2d` and digits start at `0x30`.

But look at the negatives among themselves: `-000001` before `-000020`, i.e.
**-1 before -20**. Backwards. Larger magnitude means *smaller* value once you're
below zero, so the digits have to sort in reverse — and they don't.

There's a second, quieter problem: fixed width means a fixed range. `1e40` needs 41
digits. Pick width 20 and you either truncate (silent corruption) or raise on a
value the API said was legal.

### Attempt 3 — pack it as a float64, big-endian

```
  float64 BE  ->  0, 2, 9, 10, 100, 1000, -1, -20
```

Positives are perfect — IEEE-754 was designed so that the bit pattern of a positive
float sorts like the value. Negatives are catastrophic: they go to the *end*, and
they're reversed among themselves.

The bytes show why:

```
   -2.0 -> c000000000000000
   -1.0 -> bff0000000000000     ← -1 > -2, but c0 > bf, so bytes disagree
    0.0 -> 0000000000000000
    1.0 -> 3ff0000000000000
    2.0 -> 4000000000000000
```

The sign bit is the *high* bit, so every negative number has a byte pattern
numerically larger than every positive one. And within the negatives, larger
magnitude means a larger pattern — the reverse of what we want.

And even if you fixed the ordering, `float` is disqualified for a much more basic
reason, covered in the previous doc: `12345678901234567890` round-trips as
`12345678901234567168`. Verified — off by 722. A key that doesn't round-trip is not
a key.

### Attempt 4 — 8-byte two's-complement integer, big-endian

```
  int BE  ->  0, 2, 9, 10, 100, 1000, -20, -1
```

Same disease, and the hex makes it obvious:

```
   -1 -> ffffffffffffffff
    0 -> 0000000000000000
    1 -> 0000000000000001
```

Two's complement puts the sign in the high bit, so `-1` becomes the *largest*
possible byte pattern. Also: 8 bytes caps you at ±2⁶³, and DynamoDB numbers are
38 significant digits with an exponent range — not an `int64`.

---

## 4. What the failures are actually telling you

Line up the four attempts and the requirements fall out on their own. Every fix
below is a *requirement*, not an implementation:

| Failure observed | The requirement it implies |
| --- | --- |
| `"10" < "9"` | Comparison must not stop at the first differing digit while the numbers still differ in **magnitude**. Magnitude has to be compared *before* digits. |
| Negatives sorted after positives | The **sign** must be the most significant thing in the encoding, and it must be encoded so "negative" is a smaller byte than "positive". |
| `-1` before `-20` | Within negatives, ordering **inverts**. Whatever you do for positives must be mirrored. |
| Fixed width breaks on `1e40` | The encoding must handle an arbitrary exponent range without a hard-coded width, or state its limit and enforce it. |
| `float` loses digits above 2⁵³ | Parse with `decimal.Decimal`. Every time. No exceptions. |
| `0`, `0.0`, `0.00`, `-0` | These are the **same value**. If they encode to different bytes, `GetItem` misses items that are present. Normalisation is part of the job. |

That last row is the one people forget until a test catches it. `Decimal("1.0")`
and `Decimal("1.00")` compare equal but have different string forms. If the encoded
bytes differ, you've created two addresses for one item.

The scaffold's TODO names the classic shape without building it:

```python
    #  - N: the hard one. Parse with `decimal.Decimal` (never `float` — it rounds
    #    large integers silently), then design an encoding where negative numbers
    #    sort before positive ones and "10" does not sort before "9". Sign flag +
    #    biased exponent + normalised digits is the classic shape.
```

Read that as three sub-problems, in priority order: **sign first** (it dominates),
**then magnitude** (the exponent — this is what fixes `10` vs `9`), **then the
digits**. Why the exponent has to come before the digits, what "biased" is doing
there, how you mirror all of it for negatives, and how you terminate the digit run
unambiguously — that's the V1 challenge, and it's genuinely satisfying to derive.
I'm stopping at the door.

---

## 5. How you know it's right: property testing

This is the part I'd push hardest on, because it changes what "tested" means here.

Example-based testing is nearly useless for an encoding. You'd write
`assert encode(1) < encode(2)`, it passes, and you'd have verified one pair out of
infinitely many. The bug is always in the pair you didn't think of — usually
something like `(-0.0, 0)`, `(Decimal("1.0"), Decimal("1.00"))`, or two numbers
straddling an exponent boundary.

The property you want is a direct restatement of the contract:

> For any two values `a`, `b` of the same type:
> `(encode(a) < encode(b))` **iff** `(a < b)`, and `(encode(a) == encode(b))`
> **iff** `(a == b)`.

Generate thousands of random same-type pairs and assert it. The scaffold points
straight at this:

```python
    # Property-test it: for random same-type pairs, byte order must match value
    # order. That test is the V1 proof.
```

Regions worth making sure your generator actually reaches — a uniform random
integer generator will miss most of them:

- zero, and negative zero
- values straddling zero (`-1`, `0`, `1`)
- the same value written differently (`1`, `1.0`, `1.00`, `1e0`)
- adjacent magnitudes (`9` vs `10`, `99` vs `100`) — where attempt 1 died
- very large and very small exponents
- the empty string, and strings where one is a prefix of the other

A round-trip property (`decode(encode(v)) == v`) is worth having alongside it if
you decode anywhere — an encoding can be perfectly ordered and still lossy.

---

## 6. Composing the two halves

Once each part encodes correctly, the composite key has one more requirement,
which the previous doc demonstrated:

```
  ("a",    "bc")  ->  b'abc'
  ("ab",   "c" )  ->  b'abc'        ← collision
  pk="a\x00b", sk="c"   ->  b'a\x00b\x00c'
  pk="a", sk="b\x00c"   ->  b'a\x00b\x00c'    ← collision even with a delimiter
```

*(Both verified.)* So the composite encoding must be **unambiguous**: distinct
`(pk, sk)` pairs must never produce identical bytes, no matter what byte values
appear inside the key values themselves.

There's a real tension here worth naming, because it's the design decision:

- The composite bytes must still be **order-preserving on the sort key** *within a
  partition* — otherwise your sorted-partition invariant collapses and `between`
  breaks again.
- But partitions are addressed by a **hash** of the partition key, so ordering
  *across* partitions isn't required at all.

Which means you have a choice about whether the partition key and sort key are ever
concatenated into one blob, or kept as separate levels (partition addressed by one
encoding, items within it ordered by the other). The scaffold's suggested shape —

```python
    #   self._partitions: dict[bytes, list[tuple[bytes, Item]]]
```

— hints at the second, but it is a decision you should be able to defend, and
`docs/23-design.md` asks you to record it.

One more consequence to be aware of before you pick: `begins_with` must work on the
sort key. If a prefix of the *encoded* form doesn't correspond to a prefix of the
*value*, `begins_with` becomes hard. For UTF-8 strings it's natural. For your
number encoding, ask yourself whether `begins_with` on a number even means
anything — and if the answer is "no", make the API say so rather than returning
nonsense.

---

## 7. Where this shows up later (it's not just V1)

`encode_key` is load-bearing for four other verticals. Getting it right once pays
out repeatedly; getting it wrong means debugging it through three layers of
abstraction:

| Vertical | Depends on the encoding for |
| --- | --- |
| **V2** indexes ([indexes.py](../src/dynamodb_core/indexes.py)) | Index entries are items in a differently-keyed table. Same encoding, new key schema — and the "changed indexed attribute" case is *detected* by the old and new encoded keys differing. |
| **V3** conditions ([conditions.py](../src/dynamodb_core/conditions.py)) | Transactions reject duplicate `(table, key)` legs — which requires a canonical form for "the same key". |
| **V4** capacity ([throughput.py](../src/dynamodb_core/throughput.py)) | The per-partition token bucket is keyed by the encoded **partition** key. Two spellings of one key ⇒ two buckets ⇒ double the real capacity. |
| **V5** streams ([streams.py](../src/dynamodb_core/streams.py)) | One shard per partition key, per-key ordering. Shards are addressed by encoded partition key — the scaffold's `dict[bytes, deque[StreamRecord]]` says so. |

Same trap in all four: **non-canonical encodings silently fork identity.** If
`Decimal("1")` and `Decimal("1.0")` encode differently, you get two items, two
buckets, and two stream shards for what the user thinks is one thing.

---

## 8. Mental-model summary

| The instinct | The correction |
| --- | --- |
| "I'll compare the values directly, no encoding needed." | Keys go to disk, into a WAL, and get composed into pairs. Bytes are the common currency. |
| "Numbers as strings is a weird API choice." | It's precision preservation. JSON floats lose digits above 2⁵³ — verified at 722 off. |
| "Zero-padding fixes numeric sorting." | Only for non-negatives of bounded width. Negatives sort backwards among themselves. |
| "IEEE-754 bits sort correctly." | For positives only. The sign bit is high, so all negatives sort after all positives, reversed. |
| "Two's complement is the standard, it'll be fine." | `-1` is `ff…ff` — the largest byte pattern there is. |
| "I'll test it with a few examples." | Test the **property** over random pairs. The failing pair is always the one you didn't imagine. |
| "`1.0` and `1.00` are obviously the same key." | Only if you normalise. Otherwise you've created two addresses for one item. |
| "A delimiter makes composite keys unambiguous." | Not if a key value can contain the delimiter. Both collisions above are real. |

---

## 9. Where you'll build this

**Module:** [table.py](../src/dynamodb_core/table.py), the `encode_key` function —
currently:

```python
raise NotImplementedError("V1: order-preserving key encoding for S / N / B")
```

Everything in `Table` sits on top of it: `put_item` places by encoded key,
`query` seeks range bounds by encoded key, and the sorted-partition invariant is
only an invariant if the encoding holds.

**Done-when criteria this doc unlocks** (from [SPEC.md](../SPEC.md) V1):

- `Query` returns items **ordered by sort key** with working `begins_with`,
  `between`, `>`, `<`
- attribute types round-trip losslessly, **numbers keeping their precision**
- and the Proof line asks for exactly this: *"unit tests for key encoding, sort-key
  ordering (including negative and large numbers)"*, plus `docs/23-design.md`
  recording **"the key encoding you chose and why sort order survives it."**

Write that design-doc paragraph as you go, not at the end. If you can't explain why
the order survives, it probably doesn't.

**Stuck?** `/hint` gives graduated nudges; `/quest` runs the vertical with
acceptance tests written before you implement, so the tests can't encode the answer.

**Next:** [02-how-conditional-writes-work.md](02-how-conditional-writes-work.md) —
the SPEC's suggested order of attack puts V3 before V2, because index maintenance
has to respect conditional writes.
