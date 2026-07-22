# How Erasure Coding Works — From First Principles

> A beginner-friendly, ground-up guide to **erasure coding**: how a store can lose
> whole disks and still hand back your bytes *exactly*, at a fraction of the cost
> of keeping extra copies. We build the whole chain the SPEC's "From the field"
> backlog names — **Reed–Solomon RS(4,2)** → **Local Reconstruction Codes (LRC)**
> → a **durability ("nines") calculator** — from nothing but XOR and a little
> byte arithmetic. No coding theory, linear algebra, or finite-field background
> assumed. It helps to know V1's content-addressed store (a blob's name is the
> hash of its bytes).
>
> **This is a teach-ahead doc.** Unlike the other deep dives, there is *no
> erasure-coding module in `src/` yet* — this explains the concept and the design
> you'd build, the same way [`docs/03-how-fuse-mountpoint-works.md`](03-how-fuse-mountpoint-works.md)
> taught FUSE before any code. It anchors to the *real* code the future coder
> would sit on top of:
> [`src/store/mod.rs`](../src/store/mod.rs) / [`src/store/file_cas.rs`](../src/store/file_cas.rs)
> (the content-addressed blob layer erasure coding would replace),
> [`src/streaming.rs`](../src/streaming.rs) (`CheckSumAlgorithm` — how corruption
> is *noticed*, the precondition every repair scheme assumes),
> [`docs/04-how-continuous-scrubbing-works.md`](04-how-continuous-scrubbing-works.md)
> (the auditor that turns "a shard is silently wrong" into "a shard is known
> missing"), [`SPEC.md`](../SPEC.md) (the RS / LRC / calculator backlog entries),
> and [`RESEARCH.md`](../RESEARCH.md) §Part 3 (the industry derivation this
> distills).

---

## 0. The one sentence to hold onto

**Split an object into `k` data shards, compute `m` extra parity shards, scatter
all `n = k+m` across different disks — and engineer the math so that *any* `k` of
the `n` shards rebuild the original exactly. Lose any `m`, lose nothing.**

That's it. Everything below is *why such math exists*, *how you compute the
parity*, *how you rebuild*, and *how to turn `(k, m)` into a durability number you
can defend.*

---

## 1. The problem: keeping copies is expensive

Start where V1 left you. Your store holds a blob once, named by its SHA-256
([`src/store/file_cas.rs`](../src/store/file_cas.rs)). One copy on one disk. The
disk dies — a head crash, a controller fault, a dropped drive — and the blob is
**gone**. Content addressing gave you dedup and integrity, but not survival.

The obvious fix is **replication**: keep 3 full copies on 3 different disks. Now
any 2 disks can die and you still have the data. Simple, and it even helps read
throughput (read whichever copy is least busy). But look at the bill:

```
Store 1 TB with 3× replication  →  3 TB of disk consumed.   Overhead: 200%.
                                    Survives: any 2 failures.
```

You are paying to store the *entire object three times* just to survive losing
two copies. At petabyte-and-up scale that 200% tax is the dominant cost of the
whole system. The question erasure coding answers is:

> Can I survive 2 failures for far less than 200% extra storage — ideally without
> storing any whole extra copy at all?

The answer is yes, and the trick is to stop thinking in *copies* and start
thinking in *equations*.

**A 2-number intuition first.** Suppose your "object" is two numbers, `a = 3` and
`b = 5`. Store a third number `p = a + b = 8` on a third disk. Now lose any one of
the three:

- Lost `a`? Recover it: `a = p − b = 8 − 5 = 3`.
- Lost `b`? Recover it: `b = p − a = 8 − 3 = 5`.
- Lost `p`? Recompute it: `p = a + b`.

You survived **any 1 failure** by storing **one** extra number for **two** data
numbers — 50% overhead, not 100%. `p` isn't a copy of anything; it's a *summary*
that, combined with the survivors, pins down the missing piece. Erasure coding is
this idea, scaled up (many data shards, several parities) and made exact for
bytes (arithmetic that never overflows or rounds).

---

## 2. Reed–Solomon RS(4,2), from scratch

**Reed–Solomon (RS)** is the classic erasure code. `RS(k, m)` means: `k` data
shards, `m` parity shards, `n = k + m` total, survive any `m` losses. Our lab
target is **RS(4,2)**:

```
        object bytes
             │  split into 4 equal data shards
   ┌────┬────┼────┬────┐
   d0   d1   d2   d3            (k = 4 data shards, stored verbatim)
   │    │    │    │
   └────┴──[ encode ]──┘
             │
          p0   p1               (m = 2 parity shards, computed)

   store all n = 6 shards on 6 different disks/failure domains
   overhead = n/k = 6/4 = 1.5×  (150% of the data, i.e. 50% extra)
   survives = any 2 of the 6 lost
```

Compare the ledger with replication:

| Scheme | Store 1 TB as | Overhead | Survives |
| --- | --- | --- | --- |
| 3× replication | 3 TB | 200% | any 2 failures |
| **RS(4,2)** | **1.5 TB** | **50%** | **any 2 failures** |
| RS(17,3) (Backblaze) | 1.18 TB | 17.6% | any 3 failures |

Same failure tolerance as 3× replication, at **a quarter of the extra storage** —
and notice the trend: the bigger `k` gets, the smaller the overhead ratio, because
`m` parities are amortized over more data. Backblaze runs `k=17, m=3` in
production for 17.6% overhead (RESEARCH.md §Part 3). RS(4,2) is the small,
hand-checkable version you build first.

### 2.1 The one hard question: why does *any* k of n work?

With replication it's obvious why you survive — you literally kept spare copies.
With RS there are no copies; `d0..d3` are stored as-is and `p0, p1` are strange
derived bytes. Why can *any 4 of the 6* possibly be enough? This is the whole
intellectual core, so we derive it.

Model the `k=4` data shards as a vector, and **encoding as one matrix multiply**.
Pick an `n×k` (here `6×4`) **generator matrix `G`** whose top `k×k` block is the
identity matrix:

```
        G  (6×4)          d (4×1)      codeword (6×1)
   ┌ 1  0  0  0 ┐        ┌ d0 ┐        ┌ d0 ┐   ← data shards, stored unchanged
   │ 0  1  0  0 │        │ d1 │        │ d1 │      (this is what "systematic"
   │ 0  0  1  0 │   ·    │ d2 │   =    │ d2 │       means: data appears verbatim)
   │ 0  0  0  1 │        │ d3 │        │ d3 │
   │ v00 v01 v02 v03 │                 │ p0 │   ← parity = a weighted mix
   └ v10 v11 v12 v13 ┘                 └ p1 ┘       of all four data shards
```

The identity rows are why data is stored verbatim (a **systematic** code — cheap
reads: no decode when nothing failed). The bottom `m=2` rows are parity
coefficients we'll choose in a moment.

Now the magic. Suppose you lose **any 2 shards** — say `d1` and `p0`. Reading is
"which shards survived?" You have 4 survivors: `d0, d2, d3, p1`. Each survivor is
one row of `G` times the unknown `d`. Stack just those 4 rows into a `4×4` matrix
`G'`:

```
   G' · d = (the 4 surviving shard values)
```

Four equations, four unknowns (`d0..d3`). If `G'` is **invertible**, then:

```
   d = G'⁻¹ · (surviving shards)
```

— you solve the system and recover *all four* original data shards, and from them
recompute any lost parity. **Reconstruction is just solving a linear system.**

So "any `k` of `n` reconstruct" is exactly the requirement:

> **Every possible `k×k` submatrix of `G` (any choice of `k` surviving rows) must
> be invertible.**

That is the entire design problem of Reed–Solomon. It is not magic — it is "pick
a matrix where you can never draw `k` rows that are linearly dependent."

### 2.2 The matrix that guarantees it

Matrices with the "every square submatrix is invertible" property are known. The
classic is the **Vandermonde matrix**, whose rows are successive powers of
distinct numbers `x_i`:

```
   row_i = [ x_i⁰  x_i¹  x_i²  x_i³ ] = [ 1  x_i  x_i²  x_i³ ]
```

A square Vandermonde matrix with *distinct* `x_i` has a nonzero determinant, so
it's invertible — and any subset of rows is again a Vandermonde with distinct
nodes, hence also invertible. That's the property we needed, for free. (In
practice people often use a **Cauchy** matrix instead, which guarantees *every*
submatrix is invertible even more cleanly, and is what several production coders
use — RESEARCH.md §Part 3 notes "Vandermonde (or Cauchy)". Same idea, sturdier
construction.)

There's just one problem left, and it's the one that makes erasure coding a
*systems* topic and not just a linear-algebra exercise: **what number system do
these matrix entries and shard bytes live in?**

---

## 3. Why the arithmetic must live in GF(2⁸)

A shard is bytes. The encode multiplies bytes by matrix coefficients and adds
them; the decode inverts a matrix and multiplies again. For "recover the bytes
*exactly*" to be true, this arithmetic needs three properties ordinary integer
math does **not** have:

1. **Closed over one byte.** A byte in must give a byte out — `0..255` → `0..255`,
   always. Ordinary `a * b` overflows past 255 immediately.
2. **Exactly invertible.** Decoding multiplies by `G'⁻¹`. If any operation loses
   information (rounding, truncation, overflow-wraparound that isn't a clean
   inverse), you can't recover the exact original bits.
3. **Normal algebra rules.** Add, subtract, multiply, and *divide* (needed to
   invert the matrix) must all behave, with associativity/distributivity, so the
   Vandermonde argument holds.

The structure that provides all three over exactly 256 values (one per byte) is a
**finite field** called **GF(2⁸)** — the "Galois field" of 256 elements. Think of
it as: *a self-contained arithmetic universe with exactly 256 numbers, where +, −,
×, ÷ all exist and never leave the universe.* Two surprises:

### 3.1 Addition is XOR

In GF(2⁸), **addition = bitwise XOR**:

```
   0x53 + 0xCA  =  0x53 ⊕ 0xCA  =  0x99
```

And because XOR is its own inverse (`x ⊕ x = 0`), **subtraction is also XOR** —
addition and subtraction are the *same operation*. This is why parity is so cheap:
a plain "XOR everything together" parity (like RAID-5) is literally a
Reed–Solomon parity with all-`1` coefficients. Hold that thought — we use it in §4.

### 3.2 Multiplication is polynomial-mod, precomputed into a table

Multiplication is the interesting one. You treat each byte as a little polynomial
(bit `i` = coefficient of `xⁱ`), multiply the polynomials, then take the remainder
modulo a fixed **irreducible polynomial** — for GF(2⁸) the standard is
`x⁸ + x⁴ + x³ + x² + 1`, which as a bitfield is `0x11D`. The modulo step is what
folds any result back into a single byte.

You never do that by hand at runtime. The one primitive you need is **"multiply by
2"** (called `xtime`): shift left one bit, and *if* the result spilled past 8 bits,
XOR it with `0x11D` to fold it back in:

```
   xtime(a):
       a <<= 1
       if a & 0x100:        # bit 8 set → overflowed the byte
           a ^= 0x11D       # fold back into GF(2⁸)
       return a & 0xFF
```

Watch the powers of 2 march through the field (generator `g = 2`):

```
   1, 2, 4, 8, 16, 32, 64, 128,  then 128×2 = 256 → 256 ⊕ 0x11D = 0x1D (29),
   29×2 = 58, 58×2 = 116, …                       └ the fold in action ┘
```

Starting from 1 and repeatedly multiplying by 2, you cycle through **all 255
nonzero bytes** before returning to 1 (that's what makes `2` a *generator*). That
fact lets you precompute two 256-entry tables once at startup:

- `log[x]` = the power `i` such that `2ⁱ = x`
- `antilog[i]` = `2ⁱ`

and then **any** multiplication is three table lookups and an add:

```
   a · b  =  antilog[ (log[a] + log[b]) mod 255 ]
```

So the "scary" finite-field multiply is, in the hot path, a table lookup —
which is exactly why Backblaze's open-source `JavaReedSolomon` hits ~149 MB/s
single-threaded on commodity hardware (RESEARCH.md §Part 3). GF(2⁸) is not slow;
it's a lookup table.

**Recap of §2–§3:** encoding = multiply data by a Vandermonde/Cauchy generator
matrix *in GF(2⁸)*; decoding = invert the surviving `k×k` submatrix *in GF(2⁸)*
and multiply. Now let's do it with real numbers.

---

## 4. RS(4,2) worked by hand — encode, kill two shards, rebuild

We'll use the **P + Q** construction (the same one Linux RAID-6 uses — it is
precisely a systematic Reed–Solomon code with `m = 2`). Two parity rows:

```
   p0  (call it P) : coefficients [1, 1, 1, 1]   → P = d0 ⊕ d1 ⊕ d2 ⊕ d3   (plain XOR)
   p1  (call it Q) : coefficients [1, 2, 4, 8]   → Q = 1·d0 ⊕ 2·d1 ⊕ 4·d2 ⊕ 8·d3
                                  = [2⁰,2¹,2²,2³]   (a Vandermonde row, base 2, in GF(2⁸))
```

`P` is the cheap XOR summary; `Q` is the weighted one that makes *two* losses
recoverable. All `·` and `⊕` below are GF(2⁸) operations from §3.

### 4.1 Encode

Pick four tiny data bytes: `d0=0x10, d1=0x20, d2=0x30, d3=0x40`.

**P** = `0x10 ⊕ 0x20 ⊕ 0x30 ⊕ 0x40`:

```
   0x10 ⊕ 0x20 = 0x30
   0x30 ⊕ 0x30 = 0x00
   0x00 ⊕ 0x40 = 0x40      →  P = 0x40
```

**Q** = `1·d0 ⊕ 2·d1 ⊕ 4·d2 ⊕ 8·d3`. Compute each GF product with `xtime`:

```
   1·d0 = 0x10
   2·d1 = xtime(0x20)            = 0x40                    (no fold: 0x20 < 0x80)
   4·d2 = xtime(xtime(0x30))     = xtime(0x60) = 0xC0
   8·d3 = xtime³(0x40): 0x40→0x80→(0x80«1=0x100)⊕0x11D=0x1D→xtime(0x1D)=0x3A
   Q = 0x10 ⊕ 0x40 ⊕ 0xC0 ⊕ 0x3A
     = 0x50 ⊕ 0xC0 ⊕ 0x3A = 0x90 ⊕ 0x3A = 0xAA            →  Q = 0xAA
```

Six shards now live on six disks:

```
   d0=0x10   d1=0x20   d2=0x30   d3=0x40   P=0x40   Q=0xAA
```

### 4.2 Two disks die — reconstruct

Say the disks holding **`d0` and `d1`** both fail. Survivors: `d2=0x30, d3=0x40,
P=0x40, Q=0xAA`. We want `d0, d1` back, exactly.

Take the two parity equations and move the *known* survivors to the right side.
Because add = subtract = XOR, "moving to the other side" is just XOR-ing:

```
   From P:  d0 ⊕ d1              = P ⊕ d2 ⊕ d3
   From Q:  1·d0 ⊕ 2·d1          = Q ⊕ 4·d2 ⊕ 8·d3
```

Right sides are all known — compute them:

```
   A := P ⊕ d2 ⊕ d3        = 0x40 ⊕ 0x30 ⊕ 0x40 = 0x30
   B := Q ⊕ 4·d2 ⊕ 8·d3    = 0xAA ⊕ 0xC0 ⊕ 0x3A = 0x50
```

So we have a 2×2 system over GF(2⁸) — exactly the `k×k` submatrix from §2.1, here
`[[1,1],[1,2]]`, with unknowns `d0, d1`:

```
   d0 ⊕ 1·d1 = A = 0x30
   d0 ⊕ 2·d1 = B = 0x50
```

Its determinant is `1·2 ⊕ 1·1 = 2 ⊕ 1 = 3 ≠ 0`, so it's **invertible** — the
guarantee from §2.1 paying off. Solve: XOR the two equations to cancel `d0`:

```
   (1·d1) ⊕ (2·d1) = A ⊕ B
   (1 ⊕ 2)·d1      = 0x30 ⊕ 0x50
   3·d1            = 0x60
```

Divide by 3 (multiply by `3⁻¹` in GF(2⁸)). You can verify the answer directly:
`3·0x20 = (1⊕2)·0x20 = 0x20 ⊕ xtime(0x20) = 0x20 ⊕ 0x40 = 0x60` ✓, so:

```
   d1 = 0x20      (recovered — matches the original!)
   d0 = A ⊕ d1 = 0x30 ⊕ 0x20 = 0x10   (recovered — matches!)
```

Both lost shards came back **bit-exact**, from parity plus the survivors, with no
copy of them ever stored. That round-trip — *encode, delete any `m`, reconstruct
byte-identical* — is precisely the observable the SPEC's RS lab asks you to prove
("a blob split into 6 shards reconstructs bit-exact after any 2 are deleted").

> **The same procedure covers every loss pattern.** Lost two data shards (above),
> two parity shards (just re-encode from the intact data), or one of each (mixed)
> — in all cases you assemble the `4×4` submatrix `G'` of surviving rows, invert
> it in GF(2⁸), and multiply. The P+Q shortcut above is the hand-friendly version
> of that general matrix solve for `m=2`.

---

## 5. The crack RS leaves open: repairing one shard costs `k` reads

RS(4,2) is durable and cheap to store. But it has a nasty operational cost that
only shows up when you *run* it, and it's the reason the story doesn't end here.

**Single-disk failure is the common case** — disks die one at a time far more
often than two-at-once. So look at what repairing *one* lost shard costs. To
rebuild a single shard, the reconstruction in §4 needs `k` other shards (you must
fill a full `4×4` system). For RS(4,2) that's **4 reads to fix 1 shard**. For
Backblaze's RS(17,3) it's **17 reads to fix 1 shard**.

```
   RS(k, m):  lose 1 shard  →  read k shards  →  decode  →  write 1 shard
                              └── the repair-read fan-in ──┘
```

Those reads cross disks and often the network. And here's the kicker from
RESEARCH.md §Part 4: modern hard drives have grown ~7,000,000× in *capacity* since
1956 but only ~150× in *seek speed* — so a big fleet's scarcest resource isn't
bytes, it's **I/O**. A repair scheme that reads `k` shards for every single-disk
failure spends your scarcest resource on the *most frequent* event. At fleet scale
that repair traffic can dominate.

So the next question:

> Can I keep RS's cheap storage and strong durability, but make the *common* case
> — one lost shard — repairable by reading far fewer than `k` shards?

That is exactly what Local Reconstruction Codes buy.

---

## 6. Local Reconstruction Codes (LRC): cheap repair for the common case

**LRC** (Azure's "Erasure Coding in Windows Azure Storage", USENIX ATC 2012 — best
paper) restructures RS instead of replacing it. It's parameterized `(k, l, r)`:

- split the `k` data shards into **`l` local groups**,
- add **one local parity per group** (covers only that group),
- add **`r` global parities** (computed across *all* `k` data shards, exactly like
  RS's parities).

The insight: **most failures are a single shard, and a single shard lives in
exactly one local group — so repair it from just that group.** You only fall back
to the expensive global parities for the rare multi-failure case.

### 6.1 A small LRC on our RS(4,2) theme

Take `(k=4, l=2, r=2)`: 4 data shards, 2 local groups of 2, 1 local parity each,
2 global parities.

```
   group A            group B
   ┌───────────┐      ┌───────────┐
   d0    d1           d2    d3
    └──[⊕]──┘          └──[⊕]──┘
       LA = d0⊕d1         LB = d2⊕d3        ← local parities (one XOR each)

   G0 = 1·d0 ⊕ 2·d1 ⊕ 4·d2 ⊕ 8·d3          ← global parities (RS-style,
   G1 = 1·d0 ⊕ 3·d1 ⊕ 9·d2 ⊕ 27·d3            span all 4 data shards)

   total shards = 4 data + 2 local + 2 global = 8
```

Now lose a single data shard, say `d0`. Its local group is A = `{d0, d1, LA}`.
Since `LA = d0 ⊕ d1`, just:

```
   d0 = LA ⊕ d1        →  read 2 shards (LA and d1), one XOR, done.
```

**2 reads to repair 1 shard, versus 4 for plain RS(4,2).** That's `≈ k/l = 4/2 = 2`
— the local-group size, not `k`. The global parities `G0, G1` sit unused for this
common case; they're the insurance that still lets you survive nastier
multi-failure patterns (e.g. a whole local group plus a global loss).

### 6.2 The honest tradeoff, and the real Azure numbers

LRC is **not free** — you added 2 local parities on top of RS's 2 global ones, so
for tiny `k` the storage overhead looks *worse* than plain RS. That's the deal LRC
makes explicit:

> **Spend a little extra storage (extra local parities) to slash the common-case
> repair I/O.**

The ratio only turns favorable at larger `k`. Azure's *production* config is
**`(k=12, l=2, r=2)`**:

```
   12 data + 2 local + 2 global = 16 shards for 12 data
   overhead = 16/12 = 1.33×
   single-shard repair reads its local group of 6, NOT all 12
```

So Azure gets **6-shard repair instead of 12**, at only **1.33× storage** — the
celebrated result (RESEARCH.md §Part 3). The SPEC's LRC backlog item asks you to
*measure* this both ways: build `(k, l, r)` on top of your RS lab and count the
repair-read fan-in for a single lost shard — `≈ k/l` (LRC) vs `k` (plain RS).

**Why it's a strict extension, not a new algorithm.** The local parities are XORs;
the global parities are *ordinary RS parities* using the very same GF(2⁸) tables
and Vandermonde coefficients from §3–§4. LRC reuses all the RS machinery and just
*reorganizes* which shards each parity covers. That's why the SPEC chains it
directly onto the RS lab: you cannot build the global parities without a working
RS encoder underneath.

---

## 7. Turning `(k, m)` into a number you can defend: the durability calculator

You can now survive `m` failures. But marketing says "eleven nines of durability"
— `99.999999999%`. Where does a number like that come from, and what's *yours* for
RS(4,2)? This is the third link in the chain, and its inputs are exactly the
`(k, m)` your RS/LRC lab produced.

### 7.1 The model

Data is lost only if **more than `m` shards fail inside one repair window** —
i.e. before the system finishes rebuilding the shards it already lost. So the
inputs are (RESEARCH.md §Part 3, Backblaze's own method):

| Input | Meaning | Backblaze's value |
| --- | --- | --- |
| `k, m, n=k+m` | the code shape | 17, 3, 20 |
| **AFR** | annual failure rate per shard/disk | ≈ 0.4% (`0.00405`) |
| **repair window** | time to detect + rebuild a lost shard | 156 hours (~6.5 days) |

The logic, step by step:

```
1. p = probability ONE shard fails within a single repair window
        = AFR × (repair_window_hours / 8760)
2. P(loss in one window) = P(≥ m+1 of the n shards fail in that window)
        = Σ_{j=m+1..n}  C(n, j) · pʲ · (1−p)^(n−j)      (binomial; the j=m+1 term dominates)
3. P(survive one window) = 1 − P(loss in one window)
4. windows per year = 8760 / repair_window_hours
5. annual durability = P(survive one window) ^ (windows_per_year)
6. "nines" = number of 9s in that probability = −log10(1 − annual_durability)
```

The engine of the whole thing is step 2: you need **`m+1`** shards to fail
*together* in one window, and because `p` is tiny, each extra required
simultaneous failure multiplies in another tiny `p` — so durability is
*exquisitely* sensitive to `m`. One more parity shard is worth orders of magnitude
of nines.

### 7.2 Backblaze's 17+3 → eleven nines

Plug in `p ≈ 0.00405 × (156/8760) ≈ 7.2×10⁻⁵`, `n=20`, need `≥4` failures. The
dominant term:

```
   P(loss/window) ≈ C(20,4) · p⁴ = 4845 · (7.2×10⁻⁵)⁴ ≈ 1.9×10⁻¹³
   windows/year ≈ 8760/156 ≈ 56
   annual loss ≈ 56 · 1.9×10⁻¹³ ≈ 1.06×10⁻¹¹
   durability ≈ 1 − 1.06×10⁻¹¹ = 0.99999999999  →  ELEVEN nines
```

Backblaze's own framing: "if you store 1 million objects for 10 million years,
you'd expect to lose 1 file."

### 7.3 Now do *your* RS(4,2) — and get a smaller, honest number

Same AFR and window, but `n=6` and you now only tolerate `m=2`, so loss means
**`≥3`** shards fail in a window:

```
   P(loss/window) ≈ C(6,3) · p³ = 20 · (7.2×10⁻⁵)³ ≈ 7.5×10⁻¹²
   annual loss ≈ 56 · 7.5×10⁻¹² ≈ 4.2×10⁻¹⁰
   durability ≈ 1 − 4.2×10⁻¹⁰  →  roughly NINE nines
```

*(Order-of-magnitude, dominant-term arithmetic, stated assumptions — the actual
calculator should sum the full binomial. The point isn't the exact digit.)*

**That's the lesson.** "Erasure coding = 11 nines" is *not* a fact — it's a
*function of your `(k, m)`, your disks' AFR, and how fast you repair*. RS(4,2) with
the same disks lands around *nine* nines, not eleven, because it tolerates one
fewer failure. Backblaze themselves note there is "no industry-standard way to
calculate it" and publish their most conservative result. So the SPEC's calculator
item is really about **honesty**: a tool that takes `(k, m, AFR, repair_window)`,
computes nines Backblaze-style, and **writes its assumptions down next to the
number** in the bench doc — because a durability figure without its assumptions is
just optimism on a slide (the same point [`docs/04`](04-how-continuous-scrubbing-works.md)
makes: the math assumes you actually *notice* failures).

### 7.4 The hidden assumption LRC quietly improves

Step 1 hides a big lever: the **repair window**. The faster you detect and rebuild,
the smaller `p`, the more nines. This is the sideways way §6 feeds §7: LRC's
cheaper single-shard repair (read `k/l`, not `k`) means faster, lighter repairs of
the common failure — effectively shrinking the repair window for the dominant
case. Same `(k, m)`, faster repair assumption, more nines. A good calculator lets
you feel that: hold everything fixed, halve the repair window, watch the nines
climb.

---

## 8. How this maps onto *this* project

Erasure coding is a **storage-engine lab** that sits *under* everything you've
built, not a change to the S3 HTTP surface. If a future coder wired it in:

**What stays exactly the same:**

- **Content addressing.** A blob is still named by the SHA-256 of its bytes
  ([`src/store/file_cas.rs`](../src/store/file_cas.rs)). Erasure coding changes
  *how the bytes are physically stored*, not the object's identity or the API.
- **The crash-safe commit discipline.** Each shard is still written temp → fsync →
  rename → fsync-dir (the durable-publish dance V1 taught); "a shard is fully
  there or not there" is the per-shard version of the same invariant.
- **Noticing corruption is the precondition.** Erasure math only saves you if you
  *know* a shard is bad. That's the job of end-to-end checksums
  ([`src/streaming.rs`](../src/streaming.rs) `CheckSumAlgorithm`) plus continuous
  scrubbing ([`docs/04`](04-how-continuous-scrubbing-works.md)): the scrubber turns
  "this shard is silently wrong" into "this shard is a known erasure," which is the
  input reconstruction assumes. Without the detective, the redundancy is dead
  weight.

**What changes:**

- **PUT** stops writing one blob file. It splits the durable bytes into `k` shards,
  computes `m` parity shards (GF(2⁸) tables + generator matrix), and places all
  `n` on distinct failure domains.
- **GET** reads `k` shards (preferring the least-busy — erasure coding doubles as
  heat management, RESEARCH.md §Part 4) and reconstructs on the fly if a data shard
  is missing.
- A **background repair** task rebuilds lost shards to restore the `m`-failure
  margin — and this is where LRC's `k/l` vs `k` repair-read fan-in becomes a real,
  measurable number.

**The deliverable the SPEC wants** (its Definition-of-done demands a `bench/` with
real numbers, like [`docs/06-benchmarks.md`](06-benchmarks.md) already does for the
cold tier):

1. **RS(4,2):** split a blob into 6 shards, delete any 2, reconstruct bit-exact.
2. **LRC `(k, l, r)`:** measure repair-read fan-in for a single lost shard — show
   `≈ k/l` vs plain RS's `k`.
3. **Durability calculator:** turn *your* `(k, m, AFR, repair_window)` into nines,
   with assumptions stated in the bench doc.

---

## 9. Where to start (a nudge, not a solution)

Per this repo's rules, the code is yours to write — here's the first fork in the
road, not the map:

- **The very first decision is the GF(2⁸) layer**, because everything (encode,
  decode, both parity kinds) is built on it. Settle the reduction polynomial
  (`0x11D` is standard) and precompute the `log`/`antilog` tables once — then
  multiply is a table lookup (§3.2). Get this bit-exact against a reference before
  touching matrices; a wrong table silently corrupts *every* shard.
- Then the **generator matrix**: start with the hand-checkable **P+Q** (`m=2`)
  from §4 — it *is* RS(4,2) — before generalizing to an arbitrary Vandermonde or
  Cauchy matrix for other `(k, m)`.
- Reference implementations to read (don't copy): Backblaze's **`JavaReedSolomon`**
  (MIT) and its companion **`erasure-coding-durability`** calculator; H. Peter
  Anvin's "The mathematics of RAID-6" for the P+Q derivation; the Azure LRC paper
  for `(k, l, r)`. All are grounded in RESEARCH.md §Part 3.

The whole point: **any `k` of `n`, because the encoding matrix is built so every
`k×k` submatrix inverts, over an arithmetic (GF(2⁸)) that keeps bytes exact.**
Everything else — LRC's cheaper repair, the calculator's nines — is a
consequence of that one sentence.

---

*Anchored to real code: [`src/store/mod.rs`](../src/store/mod.rs),
[`src/store/file_cas.rs`](../src/store/file_cas.rs),
[`src/streaming.rs`](../src/streaming.rs). Concept siblings:
[`docs/04-how-continuous-scrubbing-works.md`](04-how-continuous-scrubbing-works.md),
[`docs/07-durability-review.md`](07-durability-review.md). Full industry context:
[`RESEARCH.md`](../RESEARCH.md) §Part 3–4; graded contract: [`SPEC.md`](../SPEC.md).*
