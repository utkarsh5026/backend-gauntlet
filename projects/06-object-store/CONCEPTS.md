# Concept Bank — Project 06: S3-compatible Object Store

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — Content addressing & the atomic durable commit *(V1 · `src/store.rs`)*

**The problem.** Two problems live here. First: the same bytes get uploaded endlessly (the same Docker layer, the same avatar) — name blobs by where the user put them and you store each copy again. Second, and deadlier: a crash mid-write. Write straight to `objects/<hash>` and lose power halfway, and the file *exists with the right name but half the bytes* — every future reader trusts a truncated blob. Filesystems don't give you transactions; you have to construct atomicity by hand.

**The idea.** Name each blob by the SHA-256 of its content — identical bytes collapse into one file (dedup is a *consequence*, not a feature you build). And commit through the only atomic primitive the filesystem gives you: write a temp file → `fsync` it (bytes on the platter) → `rename` onto the final path (atomic within a filesystem) → `fsync` the parent directory (the *rename itself* survives). After a crash the final name either fully exists or doesn't exist at all. Shard the directory by hash prefix (`objects/ab/cd/…`) because a flat directory with millions of entries makes lookup itself slow.

**In the wild:** Git's object store is exactly this (`.git/objects/ab/cdef…`), Docker/OCI content-addressed layers, IPFS, Nix store; the temp→fsync→rename→fsync-dir dance is in SQLite's and Postgres's crash-safety code.

**You own it when you can explain:**
- [ ] Content addressing: what changes when the hash is the name (dedup, integrity-verifiable reads, immutability) and what gets harder (you need a separate name→hash index — that's V3).
- [ ] Each step of the commit sequence and the *specific* crash it defends against — including why skipping the directory fsync can un-rename a "committed" file.
- [ ] Why `rename` is atomic within one filesystem and what breaks across mount points.
- [ ] Why "the digest already exists" means drop the temp and done — idempotent commits for free.
- [ ] The directory fan-out math: why 2-level/2-byte sharding keeps directory entries sane at a billion objects.

**Depth probes:**
- What does `write()` returning success actually guarantee? (Almost nothing — page cache.) Where do the bytes live between `write` and `fsync`?
- Content addressing means a blob can never be modified in place. Why is that a *feature* for caching and replication?

**Trap:** testing crash-safety by killing your process. `kill -9` doesn't drop the OS page cache — the real test is power loss semantics, which is *why* the fsyncs are there even though every test passes without them.

**Teach-yourself docs:**
- [`docs/04-how-continuous-scrubbing-works.md`](docs/04-how-continuous-scrubbing-works.md) — how the CAS invariant becomes a continuous at-rest auditor (detect → quarantine → never serve).
- [`docs/10-how-chunk-level-dedup-works.md`](docs/10-how-chunk-level-dedup-works.md) — optional deepening: finer-grain CDC so *similar* objects share chunks (From the field; not a graded vertical).

---

## 🧠 Card 2 — Streaming I/O & free backpressure *(V2 · `src/streaming.rs`)*

**The problem.** `let body = req.bytes().await` — one line, and your object store now buffers entire uploads in RAM. A 10 KB avatar is fine; one 5 GB video OOM-kills the box, and three concurrent 2 GB uploads do it faster. The line between a toy and a real store is exactly here: memory must be O(chunk), never O(object).

**The idea.** Pull the body one chunk at a time: enforce the running size cap, write the chunk to the temp file, feed it to *two* hashers at once (SHA-256 → the content name; MD5 → the S3 ETag), then ask for the next chunk. Backpressure appears *for free*: because you only request chunk N+1 after chunk N hits disk, a slow disk propagates delay back through TCP flow control to the client — memory can't balloon *by construction*. The unhappy paths are the real work: a client that disconnects mid-PUT or trips the cap must leave no temp file behind.

**In the wild:** every real proxy and store (nginx streaming, S3 itself, MinIO); the "two hashers in one pass" trick is standard in artifact registries.

**You own it when you can explain:**
- [ ] O(1) vs O(n) memory per request, and how concurrency multiplies the difference into an OOM.
- [ ] Why backpressure here needs no explicit mechanism — trace the chain: full TCP window ← unread socket ← unpolled body ← disk-bound write loop.
- [ ] Why buffering "just a bit more for speed" trades away exactly that property.
- [ ] The cleanup contract on every early exit (error, disconnect, cap) — and what a temp-dir full of orphans tells you.
- [ ] The download side: streaming a file out with bounded memory, and why the same logic serves `Range` requests.

**Depth probes:**
- Why must the size cap be enforced *inside* the stream loop rather than by trusting `Content-Length`? (Chunked encoding, lying clients.)
- Where does the framework's default body limit interfere, and why is disabling it *plus* your own cap the right pair?

**Trap:** believing the happy path is the feature. Streaming's value is decided entirely by its cancel/error paths — the happy path buffers fine too.

---

## 🧠 Card 3 — Flat namespace, faked folders & GC *(V3 · `src/index.rs`)*

**The problem.** S3 has no directories — `a/b/c.jpg` is one opaque key — yet every client expects to "list a folder". And once dedup exists, delete gets dangerous: two keys can point at the same blob, so deleting a key must *not* delete bytes another key still needs. Finally, crashes can happen between "blob written" and "index updated" — which half-state is survivable?

**The idea.** Listing fakes hierarchy at query time: `prefix` filters, `delimiter=/` rolls keys sharing the next path segment into "common prefixes" (the folders), plus sorted order and pagination. Writes follow one iron ordering — **blob durable first, then the index pointer** — so a crash strands an unreferenced blob (harmless garbage) but never a key pointing at nothing (a visible lie). Deletes drop only the pointer; a mark-and-sweep GC later reclaims blobs nothing references — carefully not reaping a blob whose PUT committed bytes but hasn't indexed *yet*.

**In the wild:** S3's ListObjectsV2 semantics (this is why "folders" in the console are an illusion), Git's unreachable-object GC, every refcount-vs-GC storage debate.

**You own it when you can explain:**
- [ ] Why the keyspace is flat and what prefix+delimiter listing actually computes (the common-prefix rollup, with an example).
- [ ] The blob-then-pointer invariant and *why the order is that way round* — compare the two crash outcomes.
- [ ] Why delete-is-pointer-drop follows inevitably from dedup, and why reclamation must be a separate, lazy process.
- [ ] The GC race with in-flight PUTs and at least one resolution (grace period by mtime, or a commit marker).
- [ ] Why the index update itself must be atomic (temp+rename, or an append-fsync log) — the same crash discipline one level up.

**Depth probes:**
- Refcounts instead of GC: what do they buy (immediate reclaim) and what do they cost (a counter that must be transactionally correct under concurrency and crashes)?
- How does pagination (`max-keys` + continuation token) stay correct while keys are being inserted concurrently?

**Trap:** letting the index be the source of truth about *bytes*. The blob store owns "what exists"; the index owns "what it's called" — invert that and crashes create dangling keys.

---

## 🧠 Card 4 — Multipart upload & the cursed ETag *(V4 · `src/multipart.rs`)*

**The problem.** A 5 GB upload over residential internet *will* be interrupted. Restarting from byte zero every time means it may never finish. And clients want to upload in parallel to use their bandwidth. Uploads therefore need to become resumable, parallel *sessions* — with all the state and edge cases sessions imply (retried parts, out-of-order arrival, abandoned sessions leaking disk).

**The idea.** Initiate mints an `uploadId` + staging area; parts upload independently (any order, retry overwrites); complete validates each part's ETag against what was staged, concatenates in part-number order while hashing the whole into the final blob (committed via V1, indexed via V3); abort reclaims. Compatibility hinges on the ETag formula, which is deliberately weird: single PUT → `md5(bytes)`, but multipart → `md5(concat(part_md5s)) + "-N"`. The `-N` tells clients "this was multipart — don't try to verify by re-MD5ing the object."

**In the wild:** S3 multipart (this exact protocol), which the AWS SDK/CLI auto-engages above ~8–100 MB; every S3-compatible store (MinIO, R2, GCS interop mode) must reproduce the ETag formula bit-for-bit — it's the de facto compliance test.

**You own it when you can explain:**
- [ ] Why resumability requires *server-side* session state, and what each verb (initiate/uploadPart/complete/abort) transitions.
- [ ] Why parts can arrive out of order and retried parts overwrite — the idempotency that makes flaky networks survivable.
- [ ] Both ETag formulas and why the multipart one is *not* the MD5 of the object — including what the `-N` suffix prevents clients from doing wrong.
- [ ] What `complete` must validate before assembly (part list matches staged parts, ETags match) and why.
- [ ] The leak vector of never-completed sessions and why an abort/expiry policy is a real operational need.

**Depth probes:**
- Why does the part-size minimum exist in real S3 (5 MB)? What would millions of tiny parts do to the completion step?
- Could you assemble without copying (concatenate by reference)? What does your blob layout say about that?

**Trap:** computing the multipart ETag from the assembled bytes. It *looks* more correct and breaks every S3 client — wire compatibility means matching the spec's weirdness, not improving on it.

---

## 🧠 Card 5 — FUSE veneer / Mountpoint *(From the field · optional)*

**The problem.** Object stores speak HTTP (`PUT`/`GET`/`List` + `Range`). Half the tools you want to use — training loaders, `pandas.read_parquet`, random shell scripts — speak files (`open`/`pread`/`readdir`). Rewriting every consumer to sign S3 requests is a non-starter; pretending the bucket is Ext4 is a lie that breaks on rename, mid-file writes, and locking.

**The idea.** Run a **FUSE** daemon: the kernel still receives syscalls on a mount point, but forwards them to userspace. The daemon translates `readdir` → list-with-delimiter, `stat` → HEAD/list metadata, `pread` → **ranged GET**. The bucket stays an object store; the OS only *sees* files. Industry reference: **Mountpoint for Amazon S3** (Rust + FUSE + CRT parallel ranged fetches). Deliberately not full POSIX — fail early rather than fake operations S3 cannot implement efficiently. This project's From-the-field cut is **read-only**: the read path is honest once `Range` works; writes are where object≠file gets dangerous.

**In the wild:** Mountpoint, s3fs, JuiceFS, Alluxio — the "filesystem veneer over object storage" trend for ML/analytics. Same ranged-GET backbone as Parquet footer reads and project 11 VOD seeking.

**You own it when you can explain:**
- [ ] What FUSE actually moves (syscall handling into userspace) and what it does *not* move (durability still lives in the object store).
- [ ] The translation table: `readdir` / `getattr` / `pread` → List / Head / ranged Get — and why `pwrite` mid-object has no honest mapping.
- [ ] Why ranged GETs are load-bearing: without `Range`, every seek downloads the whole object.
- [ ] Mountpoint's three tenets (efficient against S3 APIs; common view with object API; fail explicitly vs silent fake POSIX).
- [ ] Why a learning veneer starts read-only even though real Mountpoint allows sequential creates.

**Depth probes:**
- Trace `os.pread(fd, 4096, 1<<30)` on a mounted 5 GB object all the way to `206` + `Content-Range` on this project's `get_object`.
- Bucket has both key `blue` and `blue/cat.jpg` — what should `ls` show, and why can't the veneer invent a second answer?

**Trap:** treating the mount as a shared POSIX disk (locks, chmod, multi-writer random updates). Last-writer-wins object semantics still rule; the veneer is a disguise, not a new consistency model.

**Teach-yourself doc:** [`docs/03-how-fuse-mountpoint-works.md`](docs/03-how-fuse-mountpoint-works.md).

---

## 🧠 Card 6 — Index as a microservice *(From the field · optional)*

**The problem.** At laptop scale, `Arc<Index>` in the same process as the S3 HTTP
front-end is correct and simple. At S3 scale the key→location map is a separately
scaled metadata fleet (persistence + coherent cache + witness) because metadata
QPS, failure domains, and ownership diverge from "dumb disks holding bytes."

**The idea.** Keep the same index *contract* (`put` / `get` / `list` / …) but put
it behind an HTTP JSON API in a second binary (or container). The S3 front-end
owns blobs (`Store`); the index service owns pointers. A process boundary turns
crashes into partial failure: "blob durable, index RPC failed" is the distributed
twin of V3's blob-then-pointer window. `make stack` runs index + API + web
console as three containers on one compose network — no Kubernetes required.

**In the wild:** S3's front-end → index/metadata subsystem → ShardStore storage
nodes (RESEARCH Part 2); Azure's partition layer; Colossus metadata in BigTable.

**You own it when you can explain:**
- [ ] Why "index" is a role that can live in-process or out-of-process without
  changing the blob-then-pointer rule.
- [ ] What RPC adds (latency, timeouts, status codes) and what it does not fix
  (a wrong write order is still wrong).
- [ ] How killing the index mid-PUT should look to a client vs what GC must clean up.
- [ ] Why `ensure_bucket` cannot return a `PathBuf` over the wire.

**Depth probes:**
- Trace one PUT across two processes: stream → `Store::commit` → HTTP `put` →
  index JSON on disk.
- List three failure modes that exist only when the index is remote.

**Trap:** splitting early for "realism" before V3's crash invariants are solid —
you will debug networking and consistency at once.

**Teach-yourself doc:** [`docs/05-how-index-as-a-service-works.md`](docs/05-how-index-as-a-service-works.md).

---

## ⚡ Rapid-fire round

- [ ] `Range` semantics: `206` + `Content-Range`, open-ended (`bytes=a-`) and suffix (`bytes=-n`) forms, `416` with `bytes */len` — and why video seeking depends on them.
- [ ] Conditional GETs: `If-None-Match` + ETag → `304`, and what that saves (bandwidth, not disk reads).
- [ ] Path traversal: why resolving through the content-addressed layout makes `../../etc/passwd` structurally impossible rather than filtered.
- [ ] Why bucket-name rules (3–63 chars, lowercase) exist in S3 (DNS compatibility for virtual-host style).
- [ ] Why you count streamed bytes yourself instead of trusting `Content-Length`.
- [ ] Never log object bodies — a store that logs payloads is a data breach with extra steps.

## 🔗 Connects to

- The temp→fsync→rename discipline returns in project 08 (segment files), project 21 (durable state), and project 22 (SSTables/WAL) — this is where you first earn it.
- `Range` serving is the delivery backbone of project 11 (VOD streaming) — and of any Mountpoint-style FUSE veneer (Card 5).
- Content-addressing logic (hash = identity) is project 19's infohash idea in filesystem form.
