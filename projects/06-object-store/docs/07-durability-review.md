# Durability review — commit paths

> AWS-style durability review for this store's crash-sensitive writes: a threat
> list ("think like an adversary") paired with the guardrail that answers each
> threat. Borrowed from security threat modeling; see
> [`RESEARCH.md`](../RESEARCH.md) §Part 4.
>
> This is a **process artifact**, not a teach-yourself essay. Implementation
> detail lives in the linked modules; this document only records risks and the
> coarse mechanisms that cover them.
>
> Anchored to:
> [`src/durable.rs`](../src/durable.rs),
> [`src/index.rs`](../src/index.rs) (`Index::put` / `write_meta`),
> [`src/lifecycle.rs`](../src/lifecycle.rs) (`tier_blob`),
> [`SPEC.md`](../SPEC.md) (From the field → Durability & correctness practice).

---

## How to read this

For each commit path:

1. **Summary** — what becomes durable, and in what order.
2. **Threats** — crash, power loss, concurrency, or mistaken cleanup that could
   permanently lose data or serve a torn value.
3. **Guardrails** — broad mechanisms that defeat whole classes of failure
   (preferred over one-off mitigations).

A post-crash state is acceptable when it is **all-or-nothing** for the logical
operation: either the new value is fully visible and complete, or the old value
(or absence) remains. Orphan temps and temporarily duplicated bytes are fine;
half-written "final" names and lost-without-trace objects are not.

---

## Path A — Content-addressed blob publish

**Summary.** Bytes become a CAS object at `objects/<ab>/<cd>/<sha256>`. The only
legal way to make that path appear is the shared dance in
[`durable::publish_temp`](../src/durable.rs): stage → `fsync(temp)` →
`rename` → `fsync(parent dir)`. Callers wrap the stage in [`TempEntry`] so
failure unlinks the half-written temp.

**Entry points.** Streaming PUT, multipart part commit, `atomic_write` /
`atomic_write_json` / `atomic_write_sibling`, and any other path that lands bytes
under the blob tree.

| # | Threat | Guardrail |
| --- | --- | --- |
| A1 | Crash mid-write leaves a file named by the digest with truncated bytes; every future reader trusts the name. | Never write the final path in place. Stage under a temp name; only `rename` onto the digest path after `fsync(temp)`. |
| A2 | `rename` succeeds in the page cache but power loss rewinds the directory; reboot shows the old (or absent) entry while the caller already acked success. | `fsync` the **parent directory** after rename so the directory entry itself is durable. |
| A3 | `rename` across filesystems is copy+delete, not atomic — torn dest or lost source. | Same-filesystem invariant: temps live under the store/`tmp/` tree (or a sibling of `dest` via `atomic_write_sibling`). |
| A4 | Error / cancelled future leaves a half-written temp that fills the disk or confuses GC. | [`TempEntry`] RAII: unlink on drop unless [`disarm`](../src/durable.rs) after a successful publish. |
| A5 | Reordering steps (rename before fsync, skip dir fsync) silently reintroduces A1/A2. | One shared `publish_temp`; call sites do not re-derive the sequence. |

**Acceptable post-crash states.** No final path, or a final path whose bytes were
fully fsynced before the rename. Never a final path with partial content.

---

## Path B — Version pointer flip (`Index::put`)

**Summary.** Overwrite is not mutate-in-place: the blob is already durable in
the CAS, then the index row for `(bucket, key)` gains a new immutable version and
the live pointer (`latest`) flips. Persistence goes through
[`Index::write_meta`](../src/index.rs) → `atomic_write_json` → Path A's dance on
the per-key JSON under the bucket index tree. Staging uses the bucket's `tmp/`
so in-flight digests stay visible to GC.

**Order that must hold.**

1. Blob publish completes (Path A) — digest is durable and named correctly.
2. Under the per-key lock: read current meta → check precondition → append version
   / set `latest` → durable write of the whole `ObjectMeta` JSON.

| # | Threat | Guardrail |
| --- | --- | --- |
| B1 | Index JSON written in place; crash leaves torn JSON → key unreadable or wrong history. | Same publish dance as blobs (`atomic_write_json`); readers only ever see the previous complete file or the new complete file. |
| B2 | Pointer flips before the blob exists → GET 200 / open fails, or GC races the new digest. | Upload path commits the CAS blob **before** `Index::put`. The lock spans only the tiny metadata update, not the upload. |
| B3 | Two concurrent PUTs both read the same base, both append, last writer drops a version (lost update); `If-Match` CAS tears. | Per-key async mutex held across read → precondition → `write_meta`. |
| B4 | Crash after blob durable but before pointer publish → orphan blob, key still at old version. | Acceptable all-or-nothing for the key. Orphan is reclaimable by GC; prior version (or absence) remains correct. Bucket `tmp/` staging lets the mark phase see in-flight digests. |
| B5 | Crash after new index row is visible but client never saw 200 → client retries; duplicate version or precondition surprise. | Idempotency / preconditions are API concerns; durability invariant still holds (no torn row, no pointer to missing blob from a successful ack). Retry with `If-None-Match: *` / `If-Match` as appropriate. |
| B6 | Delete or history rewrite loses the only reference while the blob is still needed — or flips `latest` to a delete marker incorrectly mid-crash. | Deletes also go through `write_meta` (full-row atomic replace). Versioned delete appends a delete marker; version-specific delete removes one entry then rewrites. Same Path A durability for the JSON. |

**Acceptable post-crash states.** Old complete index row, or new complete index
row pointing only at digests that already exist in the CAS. Never a partial JSON
file at the index path.

---

## Path C — Cold-tier migration (`Lifecycle::tier_blob`)

**Summary.** A blob's **identity** stays the plaintext digest (hash-then-compress).
Physical layout moves from `objects/<h>` (raw) to `cold/<h>.zst` (zstd). Location
today is probed by existence (`locate`: hot first, then cold) — there is no
separate descriptor file to flip; publishing the cold object *is* the location
commit. Hot bytes are removed only after the cold file is durably published.

**Order that must hold** (from [`tier_blob`](../src/lifecycle.rs)):

1. If `cold/<h>.zst` already exists → unlink leftover hot if any; done (idempotent).
2. Stream hot → zstd encoder → sibling temp under the cold tree; `fsync` the temp.
3. `publish_temp(temp, cold/<h>.zst)` (Path A).
4. Only then `store.remove` the hot `objects/<h>`.

| # | Threat | Guardrail |
| --- | --- | --- |
| C1 | Compress-then-hash: cold file named by compressed digest → index still points at plaintext digest → permanent miss / broken dedup. | Hash-then-compress: cold path is `cold/<plaintext-digest>.zst`. Encoding is physical only (`Encoding::Zstd`). |
| C2 | Delete hot before cold is durable → power loss → object gone. | Strict order: cold `publish_temp` success **before** hot unlink. |
| C3 | Crash mid-compress or after cold publish but before hot unlink. | Mid-compress: orphan temp (reaped like any stage); hot intact. After cold publish: both copies exist — reads still work via `locate`; next sweep finishes unlink (idempotent). |
| C4 | Tier a blob that still backs a young key (dedup shared across ages) → "hot" object becomes cold under another key. | Eligibility uses youngest referrer (`max(last_modified)` over referrers), same mark-style discovery as GC — not per-key age alone. |
| C5 | GET opens raw hot path for a cold-only blob → nonsense bytes or not found. | Read path uses `Lifecycle::open_tiered` / `locate` (hot raw vs cold + streaming zstd decode), not bare `store.open_blob`. |
| C6 | Ranged GET on zstd assumes byte offsets into plaintext map to compressed offsets. | Cold tier is a latency tradeoff: decode from a frame boundary / full stream as implemented; do not pretend raw ranges apply to `.zst` without a frame index. |

**Acceptable post-crash states.** Hot only; cold + hot (duplicate, harmless); or
cold only. Never hot deleted without a durable cold object under the plaintext
digest name.

---

## Cross-cutting guardrails

These show up on more than one path — the "coarse mechanisms" Warfield-style
reviews prefer:

| Guardrail | Covers |
| --- | --- |
| Single `publish_temp` sequence (fsync → rename → fsync-dir) | A1–A5, B1, C2–C3 |
| `TempEntry` disarm-only-after-success | A4, C3 |
| Same-filesystem staging | A3 |
| Blob-before-pointer (CAS then index) | B2, B4 |
| Per-key lock around pointer update | B3 |
| Hash-then-compress identity | C1 |
| Cold-before-hot-delete | C2 |
| Idempotent recovery (retry tier / GC orphans) | B4, C3 |

**Out of scope for this review** (related, tracked elsewhere in SPEC):

- Crash-injection harness that kills every step boundary automatically.
- GC ↔ in-flight-PUT under Loom/Shuttle — teach-yourself:
  [`08-how-loom-and-shuttle-work.md`](08-how-loom-and-shuttle-work.md).
- Continuous scrubbing (at-rest bit rot after a successful commit) —
  [`docs/04-how-continuous-scrubbing-works.md`](04-how-continuous-scrubbing-works.md).
- Multi-replica / erasure-coded durability math (single-node disk layout).

---

## When to revisit

Re-open this review when a change:

- writes a final path without `publish_temp`,
- updates index or location metadata without the atomic JSON/blob dance,
- deletes or unlinks a still-needed representation before its replacement is
  durable,
- introduces a new physical encoding or a real location descriptor file (replace
  the probe in `locate` with an explicit flip — that flip becomes Path C step 3
  and needs its own rows here).

---

## References

- [`src/durable.rs`](../src/durable.rs) — module docs are the Path A source of truth.
- [`src/index.rs`](../src/index.rs) — `Index::put`, `write_meta`.
- [`src/lifecycle.rs`](../src/lifecycle.rs) — CAS collision note + `tier_blob` order.
- [`RESEARCH.md`](../RESEARCH.md) §Part 4 — durability reviews as a process.
- SPEC From the field — "A durability review for the commit path".
