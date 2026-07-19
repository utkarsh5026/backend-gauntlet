# How FUSE Mountpoints Work — From First Principles

> A beginner-friendly, ground-up guide to the idea behind the SPEC's
> "Mountpoint-style FUSE veneer": presenting an object bucket as a local
> filesystem whose reads are really ranged HTTP GETs. No prior knowledge of
> FUSE, kernel VFS, or Mountpoint assumed.
>
> This teaches the **concept**. It does **not** implement the From-the-field
> backlog item — that stays yours if you adopt it.
>
> Anchored to: this project's Range GET in [`src/routes.rs`](../src/routes.rs),
> the flat-key illusion in [`00-how-s3-paths-work.md`](00-how-s3-paths-work.md),
> industry notes in [`RESEARCH.md` §Part 7](../RESEARCH.md), and the optional
> SPEC line under **From the field**.

---

## 0. The one sentence to hold onto

**A Mountpoint-style mount is a translation layer: `open` / `read` / `seek` /
`readdir` become `ListObjects` + ranged `GetObject` — the bucket stays an
object store; the OS just *sees* files.**

"Veneer" means a thin disguise. File-shaped on the outside. Object semantics
underneath. Once that clicks, every detail in this document is just *how* the
disguise is built and *where* it must refuse to lie.

---

## 1. The problem this solves

Imagine you train a model. Your data lives in a bucket:

```
s3://training/shards/shard-000.parquet
s3://training/shards/shard-001.parquet
…
```

Your training code, and half the ecosystem around it (PyTorch data loaders,
`pandas.read_parquet`, random scripts someone wrote in 2019), wants this:

```python
with open("/mnt/training/shards/shard-000.parquet", "rb") as f:
    f.seek(footer_offset)
    footer = f.read(8)
```

Without a veneer, every tool must speak HTTP: sign requests, handle retries,
parse `ListObjectsV2` XML, issue `Range` headers. That is fine for a greenfield
pipeline. It is painful for everything else.

So the industry built a class of systems — **Mountpoint for Amazon S3**, s3fs,
JuiceFS, Alluxio — that answer one question:

> Can we give applications a *file API* while keeping *object economics*
> (cheap durable blobs, HTTP, no POSIX shared-disk fiction)?

Yes — if you accept that the "filesystem" is a carefully limited facade.

---

## 2. What a "file" is to the kernel

Before FUSE, forget S3. On Linux, a program never talks to "the disk" directly
for normal I/O. It makes **syscalls**. The important ones for this story:

| Syscall | Rough meaning |
| --- | --- |
| `open(path)` | "Give me a handle (file descriptor) for this path" |
| `read(fd)` / `pread(fd, buf, len, offset)` | "Copy `len` bytes starting at `offset` into my buffer" |
| `readdir` / `getdents` | "List names under this directory" |
| `stat` / `getattr` | "Size, mode, mtime — metadata, not bytes" |
| `write` / `pwrite` | "Put these bytes at this offset" (the hard part for objects) |

The kernel's **VFS** (Virtual File System) is a switchboard. Every mounted
path (`/`, `/home`, `/mnt/data`, …) is backed by some **filesystem driver**
that implements those operations. Ext4, XFS, NFS — each is a different
implementation of the same abstract interface.

```
  your process
       │  open("/mnt/bucket/model.bin")
       ▼
  ┌─────────────┐
  │  Linux VFS  │  "which filesystem owns /mnt/bucket?"
  └──────┬──────┘
         │
         ▼
  filesystem driver  →  actually fetches the bytes somehow
```

Normally that driver lives **in the kernel**. FUSE changes *where* the driver
runs — not the fact that apps still use `open`/`read`.

---

## 3. FUSE: filesystem in userspace

**FUSE** = **F**ilesystem in **U**ser**s**pac**e**.

Instead of writing a kernel module, you run a normal userspace program (a
**daemon**). The kernel still receives syscalls on the mount point, but it
forwards each operation through a special device (`/dev/fuse`) to your daemon.
Your daemon does whatever it wants — talk to S3, talk to a database, invent
files from thin air — then sends the answer back. The kernel returns that
answer to the calling process as if a "real" filesystem had answered.

```
  app:  pread(fd, buf, 4096, offset=1<<30)
           │
           ▼
  kernel VFS
           │  FUSE_READ request
           ▼
  /dev/fuse  ──────────────────────────────────┐
                                               ▼
                                    your FUSE daemon
                                      (e.g. Mountpoint)
                                               │
                                               │  HTTP GET + Range
                                               ▼
                                         object store
                                               │
                                               │  206 Partial Content
                                               ▼
                                    daemon replies to kernel
                                               │
                                               ▼
  app gets 4096 bytes in buf
```

That is the entire trick. FUSE is not magic storage. It is a **plumbing
protocol** between the kernel and a userspace translator.

A few consequences beginners trip on:

- Latency is higher than an in-kernel FS (extra context switches + your daemon's
  logic + network). Mountpoint fights this with parallelism and prefetch, not by
  pretending round-trips are free.
- If your daemon crashes or hangs, the mount looks broken — apps stuck in
  `read()` wait on *you*.
- Permissions, caching, and "what does `rename` mean?" are *your* policy. The
  kernel only asks; you decide what is allowed.

---

## 4. Object storage is not a filesystem

This project already taught the core illusion in
[`00-how-s3-paths-work.md`](00-how-s3-paths-work.md): **S3 has no folders.** A
key like `colors/blue/cat.jpg` is one flat string. Listing with
`prefix` + `delimiter=/` *fakes* a tree for humans and tools.

That flat model has deeper consequences for anything pretending to be a POSIX
disk:

| POSIX expectation | Object store reality |
| --- | --- |
| Patch bytes 100–200 in the middle of a file | Usually **replace the whole object** (or multipart-upload a new version) |
| `rename(a, b)` is a metadata update | Often **copy + delete** (or unsupported); not one atomic inode move on general buckets |
| `chmod` / ownership per file | Auth is **IAM / bucket policy**, not Unix modes on each object |
| Many writers, byte-range locks | Objects are **immutable blobs + atomic publish**, not shared mutable files |
| Directory is a first-class inode | "Directory" = **prefix convention** reconstructed at list time |

A veneer that *pretends* full POSIX while talking to S3 either:

1. **Lies** (accepts `pwrite` mid-file, buffers forever, never durable), or
2. **Fails early** (returns an error for operations that cannot map cleanly).

Mountpoint chose (2). That honesty is the product.

---

## 5. The veneer idea — file API on top, object semantics underneath

Translate each filesystem operation into the smallest honest object API:

| What the app does | What the veneer typically does |
| --- | --- |
| `ls /mnt/bucket/colors/` | `ListObjects` with `prefix=colors/` and `delimiter=/` → names become dirents |
| `stat /mnt/bucket/colors/list.txt` | `HeadObject` (or cached list metadata) → size, mtime |
| `open` + `pread` at offset O, length L | `GetObject` with `Range: bytes=O-(O+L-1)` |
| `open` + sequential `read` of a huge file | Many parallel ranged GETs + a prefetch buffer (Mountpoint/CRT) |
| Mid-file random `pwrite` | **Reject** (or only support "create new object by sequential write") |

Path mapping is the other half. Mountpoint (and friends) treat `/` in keys as
directory separators — the same illusion as S3 Console folders, now exposed
through `readdir`:

```
Object keys in the bucket:          What ls shows after mount:

  colors/blue/cat.jpg                 colors/
  colors/red/dog.jpg                    blue/
  colors/list.txt                         cat.jpg
                                        red/
                                          dog.jpg
                                      list.txt
```

Nothing new was stored as directories. The tree is inferred. Your
[`docs/00`](00-how-s3-paths-work.md) mental model applies unchanged — FUSE is
just another *consumer* of that illusion.

**Conflict example (worth memorizing):** if the bucket has both a key `blue`
*and* keys under `blue/…`, a real directory and a file cannot share a name.
Mountpoint's documented behavior: prefer the directory; the object named
`blue` becomes inaccessible through the mount. Object semantics win; the
veneer does not invent a second namespace.

---

## 6. Why ranged GETs are the whole game

Without `Range`, every `pread` of 4 KB from a 5 GB object would download
**5 GB**. That would make a filesystem veneer unusable for Parquet footers,
video seeking, or ML random access.

HTTP already solved the wire shape:

```
GET /training/shards/shard-000.parquet
Range: bytes=1073741824-1073745919

← HTTP/1.1 206 Partial Content
← Content-Range: bytes 1073741824-1073745919/5368709120
← Content-Length: 4096

<exactly those 4096 bytes>
```

Your store already implements this shape in [`get_object`](../src/routes.rs):

```rust
/// Honours a `Range: bytes=a-b` header → `206 Partial Content` + `Content-Range`
/// (serving just that slice), …
async fn get_object(/* … */) -> Result<Response, AppError> {
    let is_range = headers.contains_key(header::RANGE);
    let (start, end) = if let Some(range) = headers.get(header::RANGE) {
        let range = range.to_str()/* … */?;
        validate_range(range)?
    } else {
        (0, meta.size.saturating_sub(1))
    };
    let file = state.store.open_blob_range(&meta.digest, start, end).await?;
    // …
    let status = if is_range {
        StatusCode::PARTIAL_CONTENT
    } else {
        StatusCode::OK
    };
```

And `validate_range` parses the form the veneer needs:

```rust
/// Accepts only the form S3/`GetObject` needs here: `bytes=<start>-<end>`,
/// with both ends required …
fn validate_range(range: &str) -> Result<(u64, u64), AppError> { /* … */ }
```

So the learning path in *this* project is layered:

1. **V2 / horizontal:** serve `Range` correctly (you have this).
2. **From the field (optional):** put a FUSE daemon in front that turns
   `pread` into those `Range` requests against your endpoint.

### Prefetch — why Mountpoint is fast at sequential reads

A naive veneer: one `pread` → one ranged GET → wait. Fine for random access;
terrible for streaming a multi-GB object at disk-like throughput.

Mountpoint (built on the **AWS Common Runtime / CRT** S3 client) does the
industrial version:

- Detect sequential access.
- Issue **many concurrent ranged GETs** ahead of the reader's cursor.
- Fill a local buffer so the next `read()` hits memory, not a cold HTTP RTT.
- Scale connections to saturate instance network bandwidth (tens of GB/s on the
  right hardware — the point is the *architecture*, not a number you must hit).

Random seeks still work (a single ranged GET for the needed window), but they
do not get the same prefetch win. That matches how object storage wants to be
used: sequential throughput first, random IOPS second.

---

## 7. Mountpoint specifically

[**Mountpoint for Amazon S3**](https://github.com/awslabs/mountpoint-s3) is
AWS's open-source, **Rust**, FUSE-based file client. It mounts a bucket (or
prefix) and translates local file operations into S3 REST calls.

Its maintainers summarize the design with three **behavior tenets**
(paraphrased from their
[`SEMANTICS.md`](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)):

1. **Do not support file behaviors that cannot be implemented efficiently
   against S3 APIs.** Example: a general-purpose `rename` of a "directory" would
   mean listing and copying potentially millions of keys — reject rather than
   pretend.
2. **Present a common view of object data through file APIs and object APIs.**
   Do not invent POSIX features (mutable ownership, rich xattrs) that have no
   S3 analog and would diverge from what `aws s3 ls` shows.
3. **When POSIX and these tenets conflict, fail early and explicitly.** Better
   an `IO error` than a silent "success" that never became durable bytes in S3.

### What Mountpoint supports (high level)

| Operation class | Support |
| --- | --- |
| Sequential + random **reads** of existing objects | Yes — core mission |
| Creating **new** objects via sequential writes | Yes (upload on close / `fsync`) |
| Overwriting existing objects | Only with flags + truncate semantics; still sequential from offset 0 |
| Mid-file random writes / POSIX shared mutable files | No |
| Full POSIX locking, chmod semantics, hard links | No / extremely limited |

Mountpoint is therefore **not** "S3 as Ext4." It is "S3 with a file-shaped
read (and limited write) API for high-throughput workloads."

---

## 8. Why *this* project's SPEC says read-only

The From-the-field line in [`SPEC.md`](../SPEC.md) is deliberately narrower:

> A Mountpoint-style FUSE veneer: the bucket mounts as a **read-only**
> filesystem whose reads are served by ranged GETs — file API on top, object
> semantics underneath.

Read-only is the honest learning cut:

- **Reads map cleanly.** `stat` → HEAD/list metadata; `read`/`pread` → ranged
  GET. Your store already has the hard server-side piece.
- **Writes are where the object≠file lies get expensive.** Sequential create
  needs multipart, visibility-after-close rules, crash handling for partial
  uploads, and clear errors for every POSIX write pattern you will not support.
- A read-only mount still unlocks the motivating workloads: inspect buckets with
  `ls`/`cat`, seek Parquet/ORC footers, feed readers that only need `open`+`read`.

If you later adopt the backlog item, start read-only. Add sequential create
only when you can state Mountpoint-like tenets in your own README and mean them.

---

## 9. Worked end-to-end scenario

Suppose your local object store is at `http://127.0.0.1:8080`, bucket
`training`, and a hypothetical read-only FUSE client mounts it at
`/mnt/training`.

### Step A — list a "folder"

```bash
ls /mnt/training/shards/
```

What happens conceptually:

```
app → readdir("shards")
  → FUSE daemon
  → GET /training?list-type=2&prefix=shards/&delimiter=/
  → common prefixes + object keys become directory entries
  → ls prints:  shard-000.parquet  shard-001.parquet  …
```

No directories were created on disk in the store. Prefix listing did the work
([`00-how-s3-paths-work.md`](00-how-s3-paths-work.md)).

### Step B — read a small whole file

```bash
cat /mnt/training/readme.txt
```

```
app → open + read until EOF
  → FUSE daemon
  → GET /training/readme.txt          (no Range, or Range covering all bytes)
  → 200 OK + body
  → bytes appear on stdout
```

### Step C — seek into a huge object (the money path)

```python
# read 8 bytes near the end of a 5 GiB Parquet file (footer-style access)
offset = size - 8
os.pread(fd, 8, offset)
```

```
app → pread(fd, 8, offset)
  → FUSE_READ
  → GET /training/shards/shard-000.parquet
       Range: bytes=<offset>-<offset+7>
  → 206 Partial Content
       Content-Range: bytes <offset>-<offset+7>/<size>
  → 8 bytes returned to Python
```

On *your* server, that lands in `get_object` → `open_blob_range` → stream only
that slice. The veneer never needed the other ~5 GiB.

### Sequence diagram (one random read)

```
  App                 Kernel VFS              FUSE daemon           Object store
   │                      │                        │                      │
   │ pread(4KiB @ 1GiB)   │                        │                      │
   │─────────────────────>│                        │                      │
   │                      │ FUSE_READ              │                      │
   │                      │───────────────────────>│                      │
   │                      │                        │ GET + Range          │
   │                      │                        │─────────────────────>│
   │                      │                        │ 206 + 4KiB           │
   │                      │                        │<─────────────────────│
   │                      │ data                   │                      │
   │                      │<───────────────────────│                      │
   │ 4KiB in buffer       │                        │                      │
   │<─────────────────────│                        │                      │
```

---

## 10. Numbers worth knowing & gotchas

### Orders of magnitude (intuition, not SLAs)

| Step | Rough scale |
| --- | --- |
| Local ext4 `pread` of warm page cache | microseconds |
| Local SSD random read | ~100 µs |
| Same-region S3 GET (small object / range) | single-digit to tens of ms |
| Cross-AZ / cold starts / retries | can jump much higher |

A veneer cannot erase network RTT. It can **hide** RTT for sequential scans
via prefetch and parallel ranges. Random 4 KiB reads across a huge keyspace
will feel like "remote disk," not "NVMe."

### Gotchas that bite everyone once

- **Tiny random reads are poison.** Prefetch helps sequential scans; a
  latency-bound loop of 4 KiB seeks will thrash HTTP. Batch, map larger windows,
  or keep hot data local.
- **Metadata can lag content.** Mountpoint documents short windows where
  `stat` may be briefly stale after another client overwrites/deletes, while
  directory listings stay fresher. Caching options relax consistency further
  (TTL). Object strong consistency ≠ "every `stat` is free and instant."
- **Not a shared POSIX filesystem.** Two mounts writing the same key without
  coordination is last-writer-wins at the object layer. No distributed byte-range
  lock.
- **Key vs prefix collisions.** Object `blue` and prefix `blue/` cannot both be
  first-class under one directory name — see §5.
- **Glacier / archive classes.** Mountpoint will not magically read unrestored
  archive objects. The veneer surfaces object-store constraints; it does not
  erase them.
- **Auth still exists.** The daemon uses credentials (IAM, static keys, etc.).
  Unix file modes on the mount are a *local* convenience overlay, not S3 ACLs.

---

## 11. Mental-model summary

| The instinct | The correction |
| --- | --- |
| "Mounting S3 gives me a normal Linux disk." | You get a **file-shaped API** over **object semantics**, with deliberate gaps. |
| "FUSE stores the files." | FUSE only **forwards syscalls** to a userspace daemon; storage is still the bucket. |
| "Opening a file downloads the object." | Good veneers use **ranged GETs** so `pread` fetches only the window you need. |
| "Directories are real." | Same as S3 Console: **prefixes**, inferred at list time ([doc 00](00-how-s3-paths-work.md)). |
| "If POSIX conflicts with S3, emulate POSIX." | Mountpoint: **fail explicitly**. Lying is worse than `EIO`. |
| "The SPEC wants Mountpoint." | It wants you to *understand* a **read-only** veneer built on ranged GETs — optional From-the-field adoption, not a graded vertical. |

---

## 12. How this connects & where to go deeper

**In this repo**

| You want… | Read… |
| --- | --- |
| Flat keys & fake folders | [`00-how-s3-paths-work.md`](00-how-s3-paths-work.md) |
| Server-side `Range` → `206` | [`src/routes.rs`](../src/routes.rs) (`get_object`, `validate_range`) |
| Why `Range` is on the concept checklist | [`CONCEPTS.md`](../CONCEPTS.md) (Range rapid-fire + new FUSE card) |
| Industry framing | [`RESEARCH.md` §Part 7](../RESEARCH.md) (Mountpoint paragraph) |
| The optional adoption checkbox | [`SPEC.md`](../SPEC.md) → From the field → Mountpoint bullet |

**Outside**

- Mountpoint semantics (source of truth for behavior):  
  [awslabs/mountpoint-s3 `doc/SEMANTICS.md`](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)
- Mountpoint overview:  
  [AWS Mountpoint for Amazon S3](https://aws.amazon.com/s3/features/mountpoint/)
- Study path from this project's research notes: `object_store` crate → Mountpoint
  source → Garage → `s3s` (see RESEARCH recommendations).

**Sticky note**

> File API on top, object semantics underneath — and when those disagree, the
> object store wins.
