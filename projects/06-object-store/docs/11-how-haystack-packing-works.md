# How Haystack Packing Works — From First Principles

> A beginner-friendly, ground-up guide to **small-object packing** in the
> Facebook Haystack / SeaweedFS lineage: many tiny blobs live inside a handful
> of large append-only **volume** files, located by a compact in-memory map
> instead of one filesystem inode each. No prior knowledge of inode tables or
> `pread` assumed — but it helps if you already know V1's content-addressed
> store (hash = name) and why CDC cares about file count
> ([`docs/10`](10-how-chunk-level-dedup-works.md)).
>
> This teaches the **concept** and how to *think about* adopting the lab in
> *this* project. It does **not** implement the From-the-field backlog item —
> that stays yours. No volume format, no needle module, no `todo!()` fills.
>
> Anchored to: [`SPEC.md`](../SPEC.md) (From the field → Small-object packing),
> [`src/store.rs`](../src/store.rs) (one-file-per-digest CAS today),
> [`src/durable.rs`](../src/durable.rs) (temp→fsync→rename),
> [`docs/04-how-continuous-scrubbing-works.md`](04-how-continuous-scrubbing-works.md),
> [`docs/07-durability-review.md`](07-durability-review.md) Path A,
> [`docs/10-how-chunk-level-dedup-works.md`](10-how-chunk-level-dedup-works.md)
> (packing tension), industry notes in [`RESEARCH.md`](../RESEARCH.md) §Part 1
> and §Part 6 (Haystack → SeaweedFS).

---

## 0. The one sentence to hold onto

**Pack many small CAS objects into a few append-only volume files and keep a
compact in-RAM map `digest → (volume, offset, size)` so a GET is one lookup +
one ranged read — not one inode (and often more than one disk op) per photo.**

Once that clicks: the metadata that used to live in the filesystem's inode
table now lives in a format *you* designed to be as fat as your object needs
and no fatter. You are trading generic FS metadata for a purpose-built locator.

---

## 1. The problem this project still has

Imagine day zero. Clients PUT a million unique 4 KiB thumbnails. Your V1 store
does the right CAS thing for each one:

```
PUT → SHA-256 = aaa…aaa
      published at objects/aa/aa/aaa…aaa   (temp → fsync → rename → fsync-dir)
```

Dedup works. Sharding (`ab/cd/…`) keeps directories from melting. The S3 key
index (`index/<bucket>/objects/<key>.json`) already separates *names* from
*bytes*. So what is left to hurt?

**Each unique digest is still a filesystem file** — a directory entry, an
inode, and usually at least one seek to open it. Millions of tiny objects →
millions of inodes under `objects/`. That is the **small-file problem**.

Facebook's Haystack paper (OSDI 2010) named the same pain on NAS/NFS: serving
one photo cost a disk op to translate filename→inode number, another to read
the inode, another to read the data — "excessive number of disk operations
because of metadata lookups" ([`RESEARCH.md`](../RESEARCH.md) §Part 1). Your
layout is not NFS, but the cost class is the same: **generic filesystem
metadata per blob**, when each blob is tiny and immutable.

| Workload | Today's one-file CAS | Where it hurts |
| --- | --- | --- |
| Few large objects (VM images, video) | Fine — sharding + one open is enough | Rarely inode pressure |
| Millions of unique tiny objects | Correct, expensive | Inode count, open latency, FS cache pressure |
| Near-duplicate large files | Whole-object CAS cannot share | That is CDC's job ([`docs/10`](10-how-chunk-level-dedup-works.md)) |

Your SPEC acceptance line is an *observable outcome*, not a format:

> thousands of tiny objects occupy a handful of append-only volume files
> instead of one file each, and GET still streams each one correctly
> — [`SPEC.md`](../SPEC.md)

The world before packing is: "every digest is a path." The world after is:
"many digests share a path; a small index says *where inside*."

---

## 2. What Haystack actually is

**Haystack** (and its open-source descendant **SeaweedFS**, cited in
[`RESEARCH.md`](../RESEARCH.md) §Part 6) packs many small objects into large
append-only **volume** files. Structurally:

A volume file (say, tens or hundreds of GB) is just a sequence of records back
to back — Facebook called each record a **needle** ("finding a needle in a
haystack"):

```
[ header / magic ][ key ][ flags ][ data size ][ data bytes ][ checksum ] …
```

Roles matter more than any one binary layout:

| Piece | Job |
| --- | --- |
| Volume file | One open append-only container; few inodes for many objects |
| Needle | One object's payload plus enough framing to find the next record |
| Checksum / magic | Detect torn or corrupt tails without trusting the whole file blindly |
| In-memory index | `needle key → (volume id, offset, size)` — tens of bytes per entry |

**Write.** Append the new needle to the currently-open volume (one sequential
write, one `fsync`), then update the in-memory index. No new inode — you write
further into an already-open file.

**Read.** Look up the offset in the in-memory index, then one `pread` (or
seek+read) at that offset — one data disk operation, versus the multi-step
metadata dance of a naive per-file layout.

**Delete.** You usually cannot punch a hole cheaply in the middle of an
append-only file. Classic pattern: mark the needle deleted (tombstone / flag),
keep serving until a later **compaction** copies live needles into a new
volume and drops the old one. Reclaim is lazy; the index stops advertising the
dead key immediately.

That is the whole idea: **sequential append for writes, random-ish ranged
reads for GETs, compact RAM map instead of per-object inodes.**

---

## 3. Two maps, not one

This project's architecture already splits "what is this key?" from "where are
the bytes?" Packing only replaces the *physical* half.

```mermaid
flowchart LR
  Client -->|"bucket, key"| S3Index["Index JSON\nkey to digest"]
  S3Index -->|digest| NeedleMap["Needle index\ndigest to vol, off, size"]
  NeedleMap --> Volume["volumes/N.dat\nappend-only needles"]
```

| Map | Module today | Question it answers |
| --- | --- | --- |
| **Logical index** | [`index.rs`](../src/index.rs) — `index/<bucket>/objects/<key>.json` | What digest (and version history) does this S3 key point at? |
| **Physical locator** | [`store.rs`](../src/store.rs) — `objects/ab/cd/<digest>` *is* the map | Where do those bytes live on disk? |

Today the filesystem *is* the physical locator: `blob_path(digest)` encodes
location as a path. Under packing, that becomes an explicit structure:

```
digest → (volume_id, offset, size)
```

often held in RAM (and rebuildable by scanning volumes after a crash — see §5).

**What packing must not confuse:**

- The S3 API and `(bucket, key) → digest` index stay. Clients never see volume
  IDs.
- Content-addressing stays (next section). Digests remain the identity of
  bytes; volumes are anonymous containers.
- Index-as-a-service ([`docs/05`](05-how-index-as-a-service-works.md)) still
  owns the logical map; the blob process owns volumes and the needle map — the
  same metadata-vs-bytes split you already have.

---

## 4. CAS tension — adapt Haystack; do not copy it blindly

Classic Haystack needle keys were opaque photo IDs. **Your** blob names are
SHA-256 digests of plaintext bytes. That is a feature of V1, not an accident:

| V1 invariant | Why it matters |
| --- | --- |
| Identical bytes → same digest | Dedup |
| Digest names the content | Scrubbing / integrity without a side checksum DB |
| Blob publish before index pointer | Distributed-safe "bytes then name" |

Prefer the adaptation that keeps those invariants:

1. **Needle identity = plaintext digest.** Dedup becomes "is this digest
   already in the needle map?" instead of "does `objects/…/digest` exist?"
2. **Volume files are anonymous containers.** Digests are no longer filenames;
   they are keys in the locator map.
3. **Scrubbing changes shape, not purpose.** Today
   ([`docs/04`](04-how-continuous-scrubbing-works.md)): `rehash(file) == path`.
   Under packing: `rehash(needle payload) == digest` (and/or verify the
   per-needle checksum on read, with periodic full rehash). Quarantine still
   means "do not serve these bytes."

**Trap:** dropping content-addressing to "copy Haystack literally" with opaque
keys. You would reintroduce a separate checksum catalog, weaken dedup, and
fight every scrubber / GC assumption in this repo. Packing is a **physical
layout** under CAS, not a replacement for it.

---

## 5. How to think about write and read paths

Mental wiring only — no prescribed APIs. Callers stay digest-centric;
[`Store`](../src/store.rs) hides whether a digest is a standalone file or a
needle.

### PUT / commit

[`streaming.rs`](../src/streaming.rs) still hashes the body (and still
produces an ETag). Below a size threshold, the commit path that today calls
`publish_temp` onto `objects/ab/cd/<digest>` instead:

1. Append a needle to the open volume.
2. `fsync` the volume (durability of the append).
3. Insert `digest → (volume, offset, size)` into the in-memory map.

Large objects can stay one-file CAS (**hybrid**): packing earns its keep on
tiny unique blobs; huge objects do not benefit from sharing an inode budget
the same way.

### GET / open

`open_blob` / `open_blob_range` resolve the digest through the needle map,
then read that byte range from the volume. [`routes.rs`](../src/routes.rs),
lifecycle locate, and manifest reassembly keep talking in digests — they
should not need to know about volumes if the store abstraction holds.

### Durability fork (the hard mental shift)

Path A today ([`docs/07`](07-durability-review.md),
[`durable.rs`](../src/durable.rs)): each blob is staged, fsynced, and
**renamed** into its final name so a crash never leaves a final path with
torn contents.

If you ran temp→rename **per needle**, you would recreate one-file-per-object
and lose the packing win. Append-only volumes accept a different crash story:

| Concern | One-file CAS (today) | Packed volume |
| --- | --- | --- |
| Mid-write crash | Temp discarded; final name absent or complete | Last needle may be torn; recover by scanning / truncating the bad tail |
| Locator after reboot | Directory entries *are* the map | Rebuild needle map by walking volumes (or load a durable sidecar you design later) |
| "Final name appears only when complete" | Enforced by rename | Enforced by "index only advertises complete needles" |

Packing therefore adds a **new durability path** to reason about when you
adopt the lab — same threat-modeling habit as Path A, different mechanism.
Acceptable post-crash states stay all-or-nothing *per needle*: either the
needle is fully readable and indexed, or it is truncated away and never
served.

---

## 6. Deletes, GC, and compaction

Logical delete is unchanged: drop or tombstone the S3 key's live pointer in
the index. The digest may still be referenced by other keys (dedup) or by
other versions.

Physical reclaim under packing is different from `unlink(blob_path)`:

1. **GC mark** still starts from index roots (and manifests/chunks if CDC is
   on) — unmarked digests are dead.
2. Dead digests become **dead needles**, not deleted files. Space returns only
   when a volume is **compacted**: copy live needles to a new volume, swap the
   locator map, delete the old volume file.
3. Until compaction, disk usage can include garbage. That is normal for
   append-only designs; measure live vs packed bytes when you prove the lab.

Do not invent a fancy API here — internalize the order: **pointer drop →
unreferenced digest → dead needle → compact when worth it.**

---

## 7. Orthogonality matrix

| Technique | Optimizes for | Pressure it can create |
| --- | --- | --- |
| **Whole-object CAS** (today) | Exact-duplicate dedup; simple scrub | Many inodes for unique tiny objects |
| **CDC** ([`docs/10`](10-how-chunk-level-dedup-works.md)) | Near-duplicate *large* objects | More medium-small files (inode pressure) |
| **Haystack packing** | Inode / metadata cost of *tiny* objects | Compaction complexity; different crash path |
| **Cold zstd** ([`lifecycle.rs`](../src/lifecycle.rs)) | Bytes *inside* one blob | None for inode count; encoding ≠ layout |
| **Index-as-a-service** ([`docs/05`](05-how-index-as-a-service-works.md)) | Scaling the *logical* map | Needle map still belongs with blob IO |

**CDC vs packing.** CDC is the wrong tool for millions of unique thumbnails;
packing is the wrong tool for "two 2 GB images differ by one block." They pull
opposite directions on file count unless you later pack **chunks** into
volumes — a synthesis, not a day-one requirement.

**Compression vs packing.** Zstd changes how a needle's *payload* is stored;
packing changes whether that payload is its own file. Hash-then-compress still
applies: identity is plaintext digest; encoding is physical.

---

## 8. Suggested adoption ladder

Thinking order when you adopt the lab — not tickets, not a prescribed module
layout:

1. **Offline lab.** Write and read N tiny needles in one volume file; rebuild
   the in-memory map by scanning the volume from offset 0.
2. **Hybrid behind `Store`.** Size threshold: small digests → needle append;
   large → today's `publish_temp`. `open_blob` hides the difference.
3. **Crash.** Kill mid-append; recover by scanning and truncating a torn last
   needle; never serve an incomplete record.
4. **Tombstone + compact.** Delete logically; compact one volume so dead
   needles stop occupying bytes.
5. **Scrubber + metrics.** Rehash needle payloads; expose volume count, packed
   bytes, and (ideally) inode/file count under the data dir vs object count.
6. **Prove the SPEC.** Thousands of tiny PUTs → a handful of volume files;
   GET still streams each object correctly.

A box only flips when the Proof exists — same rule as every other SPEC line.

---

## 9. Sticky note

> Few volume files, many needles; RAM maps digest → (volume, offset, size);
> append + fsync to write; one ranged read to GET; keep CAS identity — packing
> is physical layout, not a new namespace.

---

## 10. Concepts to internalize

You own this topic when you can explain:

- [ ] Why hash sharding does not solve the small-file / inode problem.
- [ ] What a volume and a needle are, and why writes are append-only.
- [ ] The two-map split: S3 key→digest vs digest→(volume, offset, size).
- [ ] Why needle identity should stay the plaintext digest in *this* store.
- [ ] How scrubbing and dedup change shape under packing (not purpose).
- [ ] Why per-needle temp→rename would defeat packing, and what crash recovery
      looks like instead.
- [ ] Why deletes need tombstones / compaction to reclaim packed space.
- [ ] How packing relates to (and differs from) CDC and cold-tier compression.

**Depth probes:**

- Would you pack *every* blob, or only below a size threshold? What happens to
  a 2 GB object in a volume full of 4 KiB needles?
- After a crash that tears the last needle, is it safe to leave the torn bytes
  on disk if the index never points at them? What about the next append?
- If two keys share one digest (dedup), how many needles exist? How many
  locator entries?
- CDC produced 10 000 chunk files. Does packing those chunks into volumes
  conflict with content-addressed scrubbing? Why or why not?

**Trap:** treating the volume file as "the object" and putting S3 keys inside
needle headers as the only identity. Your logical index already owns keys;
needles should locate **content**, not reinvent the namespace.

---

## 11. Where to look next

| Subtopic | File / symbol |
| --- | --- |
| From-the-field acceptance line | [`SPEC.md`](../SPEC.md) § Storage-engine labs → Small-object packing |
| One-file CAS today | [`src/store.rs`](../src/store.rs) (`blob_path`, `commit_temp`, `open_blob`) |
| Durable publish dance | [`src/durable.rs`](../src/durable.rs) (`publish_temp`, `TempEntry`) |
| Streaming PUT / hash | [`src/streaming.rs`](../src/streaming.rs) |
| Logical key→digest index | [`src/index.rs`](../src/index.rs) |
| Scrubbing invariant today | [`docs/04-how-continuous-scrubbing-works.md`](04-how-continuous-scrubbing-works.md) |
| Path A durability threats | [`docs/07-durability-review.md`](07-durability-review.md) |
| CDC vs packing tension | [`docs/10-how-chunk-level-dedup-works.md`](10-how-chunk-level-dedup-works.md) |
| Index vs blob process split | [`docs/05-how-index-as-a-service-works.md`](05-how-index-as-a-service-works.md) |
| Haystack / SeaweedFS notes | [`RESEARCH.md`](../RESEARCH.md) §Part 1 (inode cost), §Part 6 (SeaweedFS) |
| Cold tier / hash-then-compress | [`src/lifecycle.rs`](../src/lifecycle.rs) |

**Scaffold status:** none for packing — unlike CDC, there is no volume module
or env flag yet. That is intentional: this doc is the concept map; the lab is
yours when you adopt it.

When you are ready to implement: keep identity = plaintext digest, hide layout
behind `Store`, pick a hybrid size threshold, invent a crash-recoverable
append + scan story, then prove "many tiny objects / few volume files" with
GETs that still round-trip.
