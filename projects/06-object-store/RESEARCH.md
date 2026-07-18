# Object Storage from First Principles: How S3 Really Works, and What's New (2023–2026)

## TL;DR
- **Object storage is a flat, immutable-object key→blob map that deliberately throws away the file system's mutable-in-place writes, hierarchical inode tree, and POSIX semantics** in exchange for near-unlimited horizontal scale and 11-nines durability. Internally, every large service (S3, Azure Blob, Colossus, Ceph) converges on the same shape: a stateless front-end fleet, a separately-scaled distributed key-value *index/metadata* subsystem, a dumb append-only *storage-node* fleet, and an asynchronous background plane (repair, GC, scrubbing, tiering).
- **The single most important recent shift is that object storage has become the *primary* storage substrate for data infrastructure**, enabled by three primitives that landed 2020–2025: strong read-after-write consistency (Dec 2020, via a "witness"/cache-coherence protocol), compare-and-swap conditional writes (If-Match/If-None-Match, 2024), and a low-latency single-AZ tier (S3 Express One Zone, 2023). Together these let Kafka (KIP-1150 "diskless"), Iceberg/Delta lakehouses, and even OLTP-style catalogs drop external lock managers and treat S3 as the source of truth.
- **For a Rust systems engineer the highest-leverage things to study are**: the S3 ShardStore SOSP'21 paper (LSM-structured, soft-updates, written in Rust, validated with lightweight formal methods + Loom/Shuttle), Reed-Solomon erasure coding over GF(2⁸), and the Rust-native codebases (Garage, Mountpoint, the `object_store` crate). An ordered study path is at the end.

## Key Findings

1. **The flat keyspace is the whole trick.** A bucket is a single flat map from a UTF-8 key string (≤1024 bytes) to an immutable blob + metadata. "Directories" (`a/b/c.parquet`) are pure convention — the `/` is just a byte in the key. With no real directory tree there is no shared mutable inode/parent node to lock, so the keyspace can be range-partitioned across independent shards. That is what makes horizontal scale possible.

2. **Immutability replaces the hardest distributed-systems problem (in-place mutation) with the easier one (garbage collection).** Objects are written whole and never modified in place; an "overwrite" is a new version that atomically flips a pointer. This makes replication, caching, and erasure coding dramatically simpler.

3. **S3 is "hundreds of microservices"** (Warfield's phrasing; a third-party estimate puts it near 350) structured as front-end fleet → index/metadata subsystem → storage-node fleet → background plane. Warfield notes "AWS tends to ship its org chart," so each box is a real org with API-level contracts.

4. **Strong consistency (Dec 2020) was retrofitted with a "witness" + cache-coherence protocol**, not by removing the cache — directly analogous to CPU cache coherence.

5. **Durability (11 nines) is an *engineered and audited* property**, not just a math output: erasure coding + continuous scrubbing + "durability reviews" (a threat-model process) + correlated-failure-aware placement + a Rust rewrite (ShardStore) validated with formal methods. Per AWS's March 2026 post, "At the heart of S3 durability is a system of microservices that continuously inspect every single byte across the entire fleet. These auditor services examine data and automatically trigger repair systems the moment they detect signs of degradation."

6. **Erasure coding beats replication on storage overhead but creates a repair-bandwidth problem**, which is why Azure invented Local Reconstruction Codes (LRC).

7. **The recent wave (2023–2026)**: S3 Express One Zone (single-AZ, session-auth, ~10× lower latency); S3 Tables (managed Iceberg); S3 Metadata; conditional writes (CAS); S3 Vectors (GA re:Invent 2025); max object size raised to 50 TB; the "diskless" rearchitecture of Kafka (KIP-1150 accepted March 2, 2026).

8. **Economics drive architecture**: Cloudflare R2's zero-egress model pressured the whole market; egress fees are why "data gravity" and multi-cloud lock-in exist.

## Details

### Part 1 — First principles: what object storage is and why it exists

**Three storage abstractions, from the bottom up.**

- **Block storage** (EBS, a raw disk/LUN) exposes a flat array of fixed-size blocks (e.g., 512 B or 4 KiB). The client (a filesystem or database) reads/writes blocks by number (LBA). It is mutable in place, low-latency, and has no notion of "files." One writer; hard to share.

- **File storage** (POSIX, NFS) builds a hierarchical namespace on top of blocks. The key data structure is the **inode**: a record holding file metadata (size, permissions, timestamps) plus pointers to data blocks. Directories are special files mapping names → inode numbers. POSIX semantics demand a lot: byte-range in-place writes, `rename()` atomicity, hard links, `fsync` ordering, readdir consistency. Every path lookup (`/a/b/c`) walks the tree, reading and often locking each directory inode. **That per-component metadata traversal is the scaling bottleneck** — exactly the "excessive number of disk operations because of metadata lookups" that Facebook's Haystack paper (OSDI 2010) called out: on NAS/NFS, serving one photo took one disk op to translate filename→inode number, another to read the inode, another to read the file.

- **Object storage** throws the tree away. The abstraction is a **flat keyspace**: `bucket → {key: (immutable blob, metadata)}`. There are exactly three core operations — PUT(key, bytes), GET(key), DELETE(key) — plus LIST(prefix). Crucially:
  - **No in-place mutation.** You cannot write byte 5000 of an object. You replace the whole object (or, since 2024, append in the Express tier). An "update" is a new immutable version + an atomic pointer flip.
  - **No real directories.** `photos/2026/cat.jpg` is a single opaque key; the slashes are just bytes. LIST with a prefix and a delimiter *simulates* a directory listing by scanning a sorted key range.
  - **Rich metadata + HTTP-native.** Each object carries system + user metadata, an ETag (content hash / opaque version tag), and is addressable via a REST API (GET is literally an HTTP GET).

**Why the flat, immutable model enables horizontal scale (the core argument).**

Consider what a POSIX rename of a directory with a million files requires: atomic update of a shared parent inode, invalidation of cached paths, lock coordination. Now consider an object PUT: it touches exactly one key. Because keys are independent and the namespace is flat, the index mapping keys→locations can be **range-partitioned**: keys `a…` on shard 1, `f…` on shard 2, etc. Each shard is an independent distributed key-value store. There is no global tree to lock, no parent-pointer contention. Adding capacity = adding shards + splitting ranges. This is the same insight that drove Google from GFS (single in-memory master → metadata bottleneck) to **Colossus** (metadata moved into BigTable, a distributed sorted KV store, letting it scale metadata "over 100x over the largest GFS clusters").

**Prefixes are not directories — and this interacts directly with partitioning.** Because the index is range-partitioned on the key string, keys sharing a long common prefix tend to land in the same partition. Historically this is why S3 users were told to add entropy early in the key (e.g., a hash prefix) to spread load — a hot prefix meant a hot partition. S3 now auto-splits partitions adaptively along the key based on observed load, not on `/` boundaries. Concretely: request rate scales at **3,500 writes/s and 5,500 reads/s per prefix**, and S3 adds capacity by splitting a busy key range into two partitions, nested as needed. Key naming = shard-key design, exactly as in any partitioned database.

**CAP / PACELC framing.** During a network partition you can't have both consistency (C) and availability (A). S3, Azure Blob, and most blob stores today choose **CP within a region for the metadata path** (strongly consistent, may reject on partition) while pushing hard on availability through redundancy. PACELC is the more useful lens: *Else* (no partition), you trade **Latency vs. Consistency**. This is exactly the tradeoff behind eventual consistency: S3 was eventually consistent for 14 years (2006–2020) precisely because a globally distributed metadata cache gave lower latency and higher availability than a strongly-coherent one.

**The consistency story in detail (the headline internals question).** Werner Vogels' "Diving Deep on S3 Consistency" (April 2021) is the authoritative account:

- Per-object metadata lives in a discrete subsystem on the GET/PUT/DELETE path: "At the core of this system is a persistence tier that stores metadata. Our persistence tier uses a caching technology that is designed to be highly resilient."
- The eventual consistency came from that cache: "on rare occasions, writes might flow through one part of cache infrastructure while reads end up querying another. This was the primary source of S3's eventual consistency."
- The fix was explicitly modeled on hardware: "CPUs implement cache coherence protocols. And that's what we needed here: a cache coherence protocol for our metadata caches that allowed strong consistency for all requests."
- They added a new component — a **witness**: it "acts as a witness to writes, notified every time an object changes. This new component acts like a read barrier during read operations allowing the cache to learn if its view of an object is stale. The cached value can be served if it's not stale, or invalidated and read from the persistence tier if it is stale."
- Witnesses are cheap because they track "a little bit of state, in-memory, without needing to go to disk," giving very high throughput at low latency. New per-object replication logic in the persistence tier lets S3 "reason about the 'order of operations' per-object," which is "the core piece of our cache coherency protocol." (AWS also holds a patent on a "witness service for ensuring data consistency in a distributed storage system," US 11,741,078.)
- Correctness was established with "deductive proofs," model checking, and model checking extended to runnable code — "provably correct, not just probably correct."

Result (Dec 1, 2020): all GET/PUT/LIST and tag/ACL/metadata ops are strongly read-after-write consistent, all regions, no price change, no performance penalty, no global dependency — which killed the need for workarounds like EMRFS Consistent View and S3Guard.

### Part 2 — The canonical internal architecture

Every large object store has the same four planes. Using S3's own vocabulary (Warfield, FAST'23 keynote / All Things Distributed):

```
        ┌─────────────────────────────────────────────────┐
Client →│  FRONT-END FLEET (stateless)                     │  REST/HTTP, auth (SigV4),
        │  request routing, throttling, TLS                │  multipart assembly
        └───────────────┬─────────────────────────────────┘
                        │
        ┌───────────────▼─────────────────────────────────┐
        │  INDEX / METADATA SUBSYSTEM  (key → location)    │  distributed persistent KV,
        │  persistence tier + coherent cache + witness     │  range-partitioned by key,
        └───────────────┬─────────────────────────────────┘  strongly consistent
                        │
        ┌───────────────▼─────────────────────────────────┐
        │  STORAGE NODE FLEET (ShardStore)                 │  dumb, append-only,
        │  millions of HDDs; erasure-coded shards          │  key→(extent, offset)
        └───────────────┬─────────────────────────────────┘
                        │
        ┌───────────────▼─────────────────────────────────┐
        │  BACKGROUND / ASYNC PLANE ("data services")      │  repair, GC, compaction,
        │  scrubbing, tiering, replication (CRR/SRR)        │  durability audits
        └─────────────────────────────────────────────────┘
```

**The index/metadata subsystem** is the brain: it maps trillions of keys to physical shard locations. Per Vogels it is a **distributed persistent key-value store** (a persistence tier + resilient caching technology), on the data path for GET/PUT/DELETE and serving LIST/HEAD. It is partitioned/routed by key prefix (range partitioning), which is why key naming affects throughput. This is the direct analog of Colossus's BigTable-backed curators, Azure's **partition layer** (a range-partitioned distributed database over blob/table/queue), and Ceph's deliberate *absence* of an index (CRUSH, below).

**Scale, for calibration** (note the dates — the numbers grow fast):
- April 2021: "well over 100 trillion objects... tens of millions of requests every second" (Vogels).
- March 2025: "hundreds of trillions of objects stored across 36 regions" (Warfield).
- **March 2026 (S3's 20th birthday, official AWS, Sébastien Stormacq): "more than 500 trillion objects... more than 200 million requests per second globally across hundreds of exabytes of data in 123 Availability Zones in 39 AWS Regions."** The same post notes the max object size grew "from 5 GB to 50 TB, a 10,000 fold increase," price dropped "approximately 85% since launch," and the service processes "over a quadrillion requests every year"; "tens of thousands of customers... each individually have objects that are spread over more than 10 million hard drives."

**ShardStore: the storage node, rewritten in Rust (SOSP 2021).** This is the layer that manages data on each individual disk — the part a Rust systems programmer will find most relatable. From Bornholt et al., "Using Lightweight Formal Methods to Validate a Key-Value Storage Node in Amazon S3":
- ShardStore is a **key-value store (>40K lines of Rust)** exposing a key→(location in extents) map. Values reference locations within **extents** (fixed-size, multi-MB regions that are the primary allocation unit).
- It is a **log-structured merge tree (LSM-tree)**, but with the shard data stored *outside* the tree (only pointers in the LSM) to reduce write amplification.
- It uses **soft updates** rather than a write-ahead log: writes are ordered so that "only writes whose dependencies are persisted are sent to disk," so any crash state on disk is consistent. Crash consistency matters here not for durability (there are already multiple erasure-coded replicas) but for **cost**: a host that loses all its data on crash triggers massive repair traffic across dozens of other nodes.
- **Validation approach** (the reason to read this paper): they wrote an **executable reference model** in Rust (~1% the size of the real system) as a specification, then used **property-based testing** to check the implementation matches the model. For concurrency they used **Loom** (sound, exhaustive interleaving checking of small correctness-critical primitives like sharded reader-writer locks) and **Shuttle** (randomized interleaving checking that scales to end-to-end stress tests). This prevented 16 issues from reaching production. The genius is "industrializing" formal methods so ordinary engineers maintain the spec on every commit.

Warfield adds they extended Rust's type safety to on-disk structures. Per AWS's March 2026 post, over the last eight years "AWS has been progressively rewriting performance-critical code in the S3 request path in Rust. Blob movement and disk storage have been rewritten, and work is actively ongoing across other components."

### Part 3 — Erasure coding, from first principles

**Replication vs. erasure coding.** Simplest redundancy: store 3 copies (3× overhead, survives 2 failures). Great for read throughput (read any copy → helps heat management), terrible for cost. Erasure coding gets the same or better durability at a fraction of the overhead.

**Reed–Solomon (RS), built up.** Split an object into **k** data shards; compute **m** parity shards; store all **n = k+m** on different failure domains. Any **k of n** reconstruct the object. Overhead = n/k. Example (Backblaze Vaults): **k=17, m=3, n=20** → survives any 3 lost shards at only **17.6% overhead** vs. 200% for 3× replication. Backblaze: "When a file is stored in a Vault, it is broken into 17 pieces, all the same size. Then three additional pieces are created that hold parity... The original file can then be reconstructed from any 17 of the 20 pieces."

*Why any k of n works — the linear algebra.* Treat the k data shards as a vector **d** = [d₀…d_{k-1}]. Multiply by an **n×k generator matrix G** whose top k×k block is the identity (a *systematic* code, so data shards are stored verbatim) and whose bottom m rows encode parity:

```
      G          d        codeword
[ 1 0 0 … 0 ]         [ d0 ]        (data shards, stored as-is)
[ 0 1 0 … 0 ]         [ d1 ]
[    …      ]  ·  d =  [ …  ]
[ 0 0 0 … 1 ]         [dk-1]
[ v00 … v0k ]         [ p0 ]        (parity shards)
[ v10 … v1k ]         [ p1 ]
```

If you lose any m shards, delete the corresponding m rows of G, leaving a k×k matrix G'. Because the parity rows come from a **Vandermonde** (or Cauchy) matrix, *any* k rows are linearly independent, so G' is invertible: **d = G'⁻¹ · (surviving shards)**. That's the whole trick — pick a matrix where every k×k submatrix is invertible.

*Why Galois fields.* All this arithmetic must be closed over fixed-width bytes (a byte in → a byte out, exactly invertible, no rounding). Ordinary integer math overflows. So RS works in **GF(2⁸)** — a finite field of exactly 256 elements, one per byte. In GF(2⁸):
- **Addition = XOR** (its own inverse; this is why parity is so cheap).
- **Multiplication** is polynomial multiplication mod an irreducible polynomial, e.g. `x⁸ + x⁴ + x³ + x² + 1` (0x11D). In practice you precompute log/antilog (exponent) tables: `a·b = antilog[(log[a] + log[b]) mod 255]`.

*Worked micro-example (the mechanics).* Generator element g=2 (0x02). Multiplying by 2 = left-shift by 1; if bit 8 sets (result ≥ 256), XOR with 0x11D to fold back into the field. So the powers of 2 go 1, 2, 4, 8, 16, 32, 64, 128, then 128·2 = 256 → 256 XOR 0x11D = 0x1D (29), and so on, cycling through all 255 nonzero elements. To encode parity for data bytes [d0, d1, d2] with coefficients from a Vandermonde row [1, 2, 4]: `p = (1·d0) XOR (2·d1) XOR (4·d2)`, where each `·` is a GF table lookup. Backblaze's open-source **`JavaReedSolomon`** (MIT) implements exactly this: "The ReedSolomon class does the encoding and decoding, and is supported by Matrix, which does matrix arithmetic, and Galois, which is a finite field over 8-bit values." It processes ~149 MB/s single-threaded on Storage Pod hardware.

**Durability math (why "11 nines").** Backblaze publishes an actual computation for its 17+3 vaults:
- Inputs: annual shard failure rate ≈ 0.4% (they use 0.00405) and a shard replacement window (a 156-hour interval / ~6.5 days).
- Data is lost only if **≥ 4 shards** (m+1) fail within the same repair window before repair completes.
- Result: probability of NOT losing data in a 156-hour window ≈ (1 − 1.89×10⁻¹³); across the 56 such intervals in a year, (1 − 1.89×10⁻¹³)⁵⁶ ≈ 0.99999999999 = **11 nines**. Conceptually: "if you store 1 million objects... for 10 million years, you would expect to lose 1 file." They note "there is no industry standard way to calculate it" and publish the most conservative result. Their open-source `erasure-coding-durability` calculator computes this from (k, m, failure-rate, repair-days). (Physical layout: a Vault spreads data across 20 Storage Pods in 20 cabinets; drives in the same position across the 20 pods form a "tome"; each file = 20 shards, one per pod.)

**The repair-bandwidth problem and Local Reconstruction Codes (LRC).** Plain RS has a nasty property: to reconstruct **one** lost shard you must read **k** other shards. With k=12, losing one disk means 12 cross-network reads — and single-disk failures are the common case. **Azure's LRC** (Huang et al., "Erasure Coding in Windows Azure Storage," USENIX ATC 2012, best paper) fixes this. A **(k, l, r) LRC** splits the k data fragments into **l local groups**, computes **one local parity per group** plus **r global parities**. Their production **(6, 2, 2)** example: 6 data fragments in 2 groups of 3, 2 local parities (one per group), 2 global parities. Now reconstructing a single lost data fragment reads only its **local group (≈k/l fragments)**, not all k. LRC achieves **1.33× overhead** while cutting reconstruction I/O — the explicit tradeoff is spending a little extra storage (an extra parity vs. pure RS) to slash the common-case repair cost. This spawned a research line: Clay codes, regenerating codes, and **minimum-storage regenerating (MSR)** codes, provably optimal in the storage-vs-repair-bandwidth tradeoff (minimize bytes transferred per repair at minimum storage overhead).

### Part 4 — Durability engineering, heat, and multi-tenancy

**Durability is a process, not a number.** Warfield's central point: the 11-nines statistical model is necessary but not sufficient. AWS layers on:
- **Durability reviews** — borrowed from security threat modeling. Any change that could affect durability posture requires writing down (a) a summary, (b) a comprehensive threat list ("think like an adversary"), (c) how the change resists each threat. They deliberately separate *risks* from *countermeasures* and favor coarse-grained **guardrails** (broad mechanisms protecting whole classes of failure) over per-risk mitigations.
- **End-to-end checksums + continuous scrubbing.** Because the disk bit-error rate is real (Warfield's plane-over-grass analogy: a drive misses ~1 bit per 10¹⁵), S3 continuously scrubs stored data; per AWS's March 2026 post, "a system of microservices that continuously inspect every single byte across the entire fleet... automatically trigger repair systems the moment they detect signs of degradation."
- **Correlated-failure awareness.** Placement spreads shards across failure domains (racks, power, AZs) so no single correlated event (power, network, fire) can take out ≥ m+1 shards — the same principle in Ceph's CRUSH failure domains and Azure's fault-domain-aware placement.
- **Formal methods as a guardrail** (ShardStore, above), now applied to the index subsystem too: automated proofs verify on check-in that consistency hasn't regressed.

**Heat management and the multi-tenancy argument (a genuinely counterintuitive result).** HDD capacity has grown ~7M× since 1956 but **seek time only ~150×** — a modern drive does ~120 random IOPS regardless of size. As drives head toward 200 TB, that's roughly **1 IOPS per 2 TB**. So the scarce resource isn't bytes, it's I/O — and "heat" (requests concentrating on a few disks) creates hotspots → tail latency. S3's answer:
- **Spread every bucket's objects across millions of disks.** A single customer's data occupies a tiny slice of any given disk, so no one workload can hotspot a disk, and any workload can *burst* to the I/O of a million drives (Warfield's example: a genomics customer bursting from thousands of Lambdas, served by >1M disks).
- **Erasure coding doubles as heat management**: k-of-n means you can read from whichever shards are least busy, steering around hot disks.
- **Workload decorrelation**: individual storage workloads are bursty (idle most of the time, sudden peaks). But aggregate millions of *independent* workloads and the aggregate demand smooths out and becomes predictable — "once you aggregate to a certain scale... it is difficult or impossible for any given workload to really influence the aggregate peak at all." **This is the deep argument for why massive multi-tenant scale gives *better*, not worse, performance than dedicated storage** — an insight impossible to exploit at small scale. Related techniques: **shuffle sharding** (giving each customer a random subset of resources so failures/abuse are isolated) and **cell-based architecture** (partitioning the fleet into independent cells to bound blast radius).

**PUT and GET lifecycles (walkthroughs).**

*PUT lifecycle:*
```
1. Front-end: terminate TLS, authenticate (SigV4 signature check), authorize (IAM/bucket policy)
2. Chunk the object; compute checksums (CRC32/CRC32C by default now)
3. Erasure-code each chunk into k+m shards
4. Placement: choose k+m storage nodes across distinct failure domains (heat- + capacity-aware)
5. Write shards; each storage node appends to an extent (ShardStore), returns location
6. Durability ack: only once enough shards are durably persisted
7. Index update: write key → shard-locations into the metadata subsystem; notify the witness
8. Return 200 (strongly consistent: a subsequent GET is guaranteed to see this write)
```

*GET lifecycle:*
```
1. Front-end: auth/authz
2. Index lookup: key → locations, via the coherent cache (witness confirms freshness; if stale, read persistence tier)
3. Read k shards (prefer least-busy shards to dodge hot disks); reconstruct if any data shard missing
4. Verify checksums end-to-end; stream bytes back
```

*Multipart upload*: initiate → upload parts in parallel (each independently retryable, min 5 MB except last) → CompleteMultipartUpload assembles parts by ETag. This is how you saturate bandwidth and how conditional writes (If-None-Match on CompleteMultipartUpload) enforce create-once.

*Ranged GET*: `Range: bytes=X-Y` reads only the needed shards/extents/offsets — the basis for parquet footer reads, `object_store` sub-range fetches, and Mountpoint's random reads.

### Part 5 — Storage classes, tiering, and cold-storage physical media

S3 classes trade latency/availability/cost: **Standard** (multi-AZ, hot) → **Standard-IA / One Zone-IA** (cheaper, retrieval fee; One Zone = single AZ, no AZ redundancy) → **Glacier Instant Retrieval** (ms access, archival price) → **Glacier Flexible Retrieval** (minutes-to-hours) → **Glacier Deep Archive** (12+ hours, cheapest). **Intelligent-Tiering** auto-moves objects between tiers by access pattern with no retrieval fees.

**What is Glacier physically?** AWS has never officially confirmed the media. The long-running, well-supported community inference is that Glacier is predominantly **high-density HDDs kept spun-down / powered-off** (and historically possibly custom low-RPM or shingled/SMR drives), which explains both the retrieval latency (drives must be powered and data marshaled) and the economics. Tape (or optical, per some patents) may play a role in the deepest tier, but **treat "Glacier = tape" as speculation, not fact** — the honest answer is the media is undisclosed and the retrieval-time SLAs are the only hard signal.

**Durability vs. availability — a distinction people conflate.** *Durability* = probability you don't permanently lose data (11 nines; about redundancy/erasure coding). *Availability* = probability you can access it *right now* (S3 Standard is designed for 99.99%; about live serving). One Zone classes have the **same durability engineering within the AZ but lower availability** because an AZ outage takes the data offline (and a full AZ loss could lose it). Cross-region: **S3 Replication (CRR/SRR)** asynchronously copies objects to another bucket/region/account (basis for at-least-once event delivery and Replication Time Control); **Multi-Region Access Points** give a single global endpoint routing to the nearest replica.

### Part 6 — Open-source and alternative implementations (architectural comparison)

- **Ceph (RADOS/RGW), CRUSH — the "no index" design.** Ceph's radical choice: instead of a lookup table mapping object→location, clients **compute** the location with the **CRUSH** algorithm. Object name → hash → **placement group (PG)** → CRUSH(PG, cluster map) → set of OSDs (disks). No central metadata server on the data path, so "clients and OSDs are not bottlenecked by a central lookup table." The **CRUSH map** encodes physical topology (host/rack/row) and rules; CRUSH pseudo-randomly but *deterministically* places replicas/EC-chunks across failure domains, and on topology change only affected PGs rebalance. **Placement groups** are the indirection layer (objects→PG→OSDs) that makes rebalancing tractable (~100 PGs/OSD rule of thumb). **BlueStore** is the modern OSD backend that writes directly to raw block devices (skipping a filesystem), storing metadata in embedded RocksDB. Contrast with S3: S3 *stores* the map (flexible placement, easy heat-steering); Ceph *computes* it (no metadata bottleneck, but placement is constrained by the function).
- **OpenStack Swift** — the other consistent-hashing design: a **ring** maps partitions→devices via consistent hashing; eventually consistent; independent account/container/object rings.
- **MinIO** — S3-compatible, Go, inline erasure coding (per-object, striped across drives in "erasure sets"). **2025 controversy**: after relicensing Apache 2.0 → AGPLv3 (2021), MinIO **stripped the admin web console** from the Community Edition (Feb 2025, replaced with a bare object browser), moved management to the paid AIStor (≈$96K/yr entry), stopped publishing Docker images (Oct 2025), and put the community repo in **maintenance mode (Dec 3, 2025)**. This drove migrations to Garage, SeaweedFS, RustFS, and Ceph. A cautionary tale about single-vendor open source vs. foundation governance.
- **SeaweedFS** — Go, explicitly based on Facebook's **Haystack** lineage; optimized for the **small-file problem** by packing many small objects into large append-only volume files with in-memory needle→offset maps (avoiding per-file inode disk ops).
- **Garage** — **Rust**, by Deuxfleurs; built for geo-distributed self-hosting on cheap/unreliable hardware. **CRDT-based metadata** (no Raft/Paxos): a Dynamo-style consistent-hash ring assigns partitions to nodes, CRDTs give coordination-free convergence, and per-partition **Merkle trees** drive anti-entropy repair. Block-level dedup, Zstd compression. Multi-crate workspace (`garage_net` → `garage_rpc` → `garage_table` → API). An excellent Rust codebase to read.
- **Apache Ozone** — Hadoop-ecosystem object store separating a namespace manager (Ozone Manager) from a block/container manager (SCM); scales past HDFS NameNode limits.
- **Backblaze B2** — 17+3 RS in Vaults (20 pods × tomes), open-source Java RS + durability calculator (above); Storage Pods are the famous commodity-drive chassis; B2's zero-egress-via-Cloudflare-partnership was an early egress disruptor.
- **Cloudflare R2** — S3-compatible, **zero egress fees** (the market-disrupting move), built on Cloudflare's edge + **Durable Objects** for strongly-consistent metadata, with erasure-coded distributed storage and a tiered read cache; designed for 11 nines. Also offers an Iceberg-compatible Data Catalog.
- **Tigris, Wasabi, Scaleway** — S3-compatible commercial stores; Tigris (built on FoundationDB) leans into globally-distributed + conditional writes; Wasabi competes on flat pricing/no-egress-with-caveats.
- **Rust-native for this user's stack**: **Garage** (full server), **`object_store`** crate (the Apache Arrow/DataFusion abstraction over S3/GCS/Azure/local — the idiomatic way to talk to object storage from Rust), **`s3s`** (an S3-protocol server framework in Rust — implement the S3 API over your own backend), and **Mountpoint** (below).

**The academic lineage worth internalizing**: GFS (2003, single-master metadata) → Haystack (OSDI 2010, small-file/needle) → f4 (OSDI 2014, warm BLOB storage with erasure coding) → Colossus (distributed BigTable metadata) → Azure Storage (SOSP 2011, stream + partition layers) → Ceph (OSDI 2006, CRUSH) → S3 ShardStore (SOSP 2021, Rust + formal methods).

**Azure Storage architecture (SOSP 2011, Calder et al.)** deserves its own note — the best-documented production blob store, a clean two-layer design:
- **Stream layer** = a GFS-like distributed append-only filesystem. Data lives in **streams** (ordered lists of **extents**); extents are sequences of append blocks. A **Stream Manager (SM)** tracks the stream namespace, extent allocation, and re-replication; **Extent Nodes (ENs)** store extent replicas. Extents are appended until sealed; **sealed extents are immutable** (enabling optimizations + erasure coding). Intra-stamp replication keeps 3 synchronous replicas.
- **Partition layer** = a **range-partitioned distributed database** on top of streams, exposing blobs/tables/queues. A **Partition Manager** assigns key ranges to **Partition Servers**; ranges split/merge/move under load. This layer gives Azure **strong consistency + the object index**.
- Azure co-designed the two layers to get **C + A + P** for the common intra-stamp partition/failure cases — the paper's explicit CAP claim.

### Part 7 — Recent innovations (2023–mid-2026), the key ask

**S3 Express One Zone / directory buckets (re:Invent 2023).** A purpose-built low-latency tier. Why it exists: to put storage in the *same AZ* as compute, eliminating cross-AZ network latency for latency-sensitive/ML/analytics workloads. Design differences from regional S3:
- **Single AZ** (you pick it; naming encodes it: `name--usw2-az1--x-s3`). No AZ redundancy → lower latency, but you must design for AZ loss.
- **Session-based auth**: instead of authenticating every request (SigV4 round-trip to IAM), `CreateSession` returns a token (read/write/read-write scope) that the SDK auto-refreshes (~every 5 min), amortizing auth latency over many requests.
- **Directory buckets** with a true **hierarchical namespace** (directories are real objects created on demand), optimized for dense directories.
- **Numbers**: consistent **single-digit-ms** first-byte latency, ~**10× faster than S3 Standard**, up to **50% lower request cost**; each directory bucket supports up to ~**200,000 reads/s and 100,000 writes/s** by default (bursting higher). Also added **append** support — a big deal, since classic S3 objects are immutable.

**S3 conditional writes / compare-and-swap (2024) — arguably the most consequential primitive for systems builders.** In Aug 2024 S3 added **If-None-Match** (create-only: succeed only if the key doesn't exist; else 412), and in Nov 2024 **If-Match** (succeed only if the object's ETag matches — i.e., it hasn't changed since you read it). *From first principles*: **compare-and-swap** is the atomic primitive `CAS(addr, expected, new)` underlying all lock-free concurrency — the write commits only if the current value equals `expected`. If-Match gives you CAS on an object using the ETag as the version token; If-None-Match gives you atomic create (a distributed mutex). Why this is huge:
- It **offloads consensus to S3.** Previously, multi-writer coordination on S3 required an external lock manager (DynamoDB, ZooKeeper, etcd). Now you can build leader election, distributed locks, and a serializable commit pointer with S3 alone.
- **Iceberg/Delta/Hudi can drop external metastores/lock managers.** Delta Lake's commit protocol needs "only one writer creates each commit file" — exactly `PutObject` with `If-None-Match`. This is why Delta's `S3DynamoDBLogStore` (a DynamoDB lock table) becomes unnecessary for multi-writer safety.
- Concrete CAS loop (the pattern), in Rust-flavored pseudocode:
```rust
loop {
    let (cur, etag) = get_with_etag("catalog/current.json").await?;
    let next = apply_change(cur);                 // compute new state
    match put_if_match("catalog/current.json", next, &etag).await {
        Ok(_) => break,                           // committed atomically
        Err(PreconditionFailed) => continue,      // someone else won; re-read & retry
    }
}
```
This single primitive is what makes "S3 as a database catalog" and "S3 as a lock service" viable — shayon.dev's "MVCC columnar table on S3" and Gunnar Morling's "leader election with S3 conditional writes" are worked examples.

**S3 Tables (re:Invent 2024) — managed Apache Iceberg.** A new **table bucket** type with **built-in Iceberg support**: AWS auto-runs **compaction** (bin-packing small files → target ~512 MB), **snapshot management/expiration**, and unreferenced-file cleanup — the toil that otherwise needs a dedicated data-eng team. Claims: **up to 3× faster queries, 10× higher TPS** than self-managed Iceberg on general-purpose buckets. Exposes an **Iceberg REST Catalog** endpoint (any Iceberg engine connects directly). Pricing note: compaction is a new line item (per-GB-processed + per-object). At re:Invent 2025, S3 Tables added **cross-region/-account replication** and **Intelligent-Tiering**. Strategic meaning: S3 goes from "stores objects" to "table-aware" — the storage layer absorbs the lakehouse.

**S3 Metadata (2024→GA).** Automatically captures object metadata into **queryable Iceberg tables** (live inventory + journal of changes), so you can SQL-query "what's in my bucket" — increasingly important for AI/RAG data discovery.

**S3 Vectors (preview July 2025, GA re:Invent 2025) — very recent, highly relevant.** The first cloud object store with **native vector storage + similarity search**. New **vector bucket** type + **vector indexes**; you store embeddings and run k-NN (cosine/euclidean) similarity queries with dedicated APIs (no separate vector DB/cluster to provision). Writes are strongly consistent. AWS's exact cost claim: it can "reduce the total cost of storing and querying vectors by up to 90% when compared to specialized vector database solutions." Latency profile at GA: "Infrequent queries continue to return results in under one second, with more frequent queries now resulting in latencies around 100 milliseconds or less" — i.e., ~100 ms, not sub-ms. Scale at GA: **up to 2 billion vectors per index... up to 20 trillion vectors in a vector bucket** (a 40× increase from the 50-million-per-index preview cap); AWS reported customers had "created over 250,000 vector indexes and ingested more than 40 billion vectors, performing over 1 billion queries (as of November 28th)." Integrates with Bedrock Knowledge Bases (RAG), SageMaker, OpenSearch, and the new Nova multimodal embeddings. Relationship to vector DBs: S3 Vectors is the cheap, massive-scale *cold/warm* vector tier; OpenSearch/Pinecone remain the low-latency hot tier — expect tiering between them.

**Other re:Invent 2025 S3 items**: **max object size raised to 50 TB** (from 5 TB); S3 Batch Operations scale (up to ~20 billion objects/job); S3 Storage Lens added performance metrics; and (security) a plan to **disable SSE-C on new buckets from April 2026** (an anti-ransomware move).

**Mountpoint for Amazon S3 (Rust FUSE client) + the FS-over-object-storage trend.** Mountpoint is a **Rust**, FUSE-based file client that presents an S3 bucket as a local filesystem, built on the **AWS Common Runtime (CRT)** S3 client. The CRT is the key performance piece: it automatically uses **multipart parallelism and byte-range fetches across many connections** to horizontally scale throughput and saturate instance bandwidth (tens of GB/s). It deliberately does **not** offer full POSIX (no random in-place writes) — it maps the file API onto object semantics. Companion: the **S3 Connector for PyTorch** (fast training-data loading). This whole trend — Mountpoint, s3fs, JuiceFS, Alluxio — is "filesystem veneer over object storage" for workloads (training, analytics) that want file APIs but object economics.

**The "diskless" / storage-compute-disaggregation thesis (the big architectural story).** The core idea: **make object storage the primary durable substrate and keep compute stateless.** Because S3 is already 11-nines durable and multi-AZ, replicating data *again* at the application layer (Kafka ISR across AZs, database replicas) is wasteful — and cross-AZ replication traffic is a huge cloud cost. So a wave of systems re-architected around S3:
- **Kafka**: **WarpStream** (2023, acquired by Confluent Sept 2024) pioneered a **diskless, brokerless** Kafka-compatible design — stateless agents write directly to S3, no local disks, no inter-AZ replication fees, up to ~24× cheaper storage, at the cost of higher latency (~hundreds of ms). **AutoMQ** keeps the Kafka codebase but swaps the storage layer for S3 + a low-latency WAL (choose ~500 ms S3-WAL or lower-latency modes per deployment). **Confluent Freight** and **Aiven Inkless** are managed variants. This culminated in **KIP-1150 "Diskless Topics," which per Aiven and the Apache Kafka wiki "passed with overwhelming support of 9 binding votes and 5 non-binding ones" on March 2, 2026** — the community officially endorsing object storage as Kafka's data layer (though production-ready upstream is years out; implementation KIPs 1163/1164 still in progress). **Redpanda Cloud Topics** is the analogous move in that ecosystem.
- **Databases**: Neon and Aurora-style **disaggregated storage** (separate the log/page store from compute); the pattern generalizes to "the log is in object storage."
- **Analytics**: **DuckDB + object storage** (query parquet directly over HTTP range reads), and the **Iceberg/Delta/Hudi table formats** that make S3 a transactional table store — all riding on conditional writes + strong consistency.
- **S3 Express One Zone's impact**: by cutting latency ~10×, it makes the diskless pattern viable for more latency-sensitive workloads (e.g., lower-latency Kafka), narrowing the gap that used to force local disks.

The tradeoff is always **latency vs. cost/operational-simplicity**: object storage adds tens to hundreds of ms of latency but removes replication cost, rebalancing pain, and stateful-node ops. For throughput-bound, elasticity-hungry workloads it's a clear win; for ultra-low-latency it isn't (yet).

**The Iceberg REST catalog wars + zero-ETL.** With Iceberg the de-facto open table format (Databricks bought Tabular; Snowflake launched Polaris/Open Catalog; AWS shipped S3 Tables' REST catalog; Databricks Unity Catalog competes), the battleground moved to the **catalog** — whoever owns the catalog owns governance and engine interop. "Zero-ETL" = query data in place on object storage (via Iceberg + engines like Athena/Trino/Spark/DuckDB) instead of copying it into a warehouse.

**Economics as architecture.** **Egress fees** (charged to move data *out* of a cloud) are the invisible hand shaping design: they create data gravity and multi-cloud lock-in. **Cloudflare R2's zero-egress** model (2021) disrupted this — per LeanOps's 2026 analysis, "Cloudflare R2 charges $0.015/GB/month for storage with absolutely zero egress fees... At 100TB with heavy egress, R2 costs roughly $1,500/month compared to $4,600+ on AWS S3," and Cloudflare's docs confirm egress "does not incur data transfer (egress) charges and is free." This pressured the incumbents; some clouds have since waived egress for customers leaving entirely (EU Data Act pressure). The architectural upshot: if egress is free, **multi-cloud and "serve directly from the bucket" become economically rational**; if it's expensive, you cache aggressively and co-locate compute.

**AI/ML-driven changes.** Object storage is now the **foundation of AI data lakes**: training data, checkpoints, and embeddings live in S3. Because GPUs starve without fast data, a caching tier sits between object storage and GPUs: **Alluxio, JuiceFS, VAST, WEKA, Quilt** — plus Mountpoint + the PyTorch connector. S3 Vectors, Nova multimodal embeddings, and Bedrock Knowledge Base integration (re:Invent 2025) push object storage further up the AI stack from passive store to active retrieval substrate.

### Part 8 — Formal methods & deterministic simulation (for the Rust engineer)

A through-line worth its own section — it's how modern storage earns trust:
- **AWS lightweight formal methods** (ShardStore): executable reference models + property-based testing + **Loom** (exhaustive) and **Shuttle** (randomized) concurrency checkers, run on every commit. Philosophy: not full verification, but automated, developer-maintainable checks that scale with the codebase.
- **Deterministic Simulation Testing (DST)**: run the whole system on a deterministic scheduler with injected faults (network partitions, disk errors, clock skew), reproducibly. **FoundationDB** pioneered this (simulation-first design); **TigerBeetle** (a financial DB in Zig) built its whole engineering culture around DST; **Antithesis** commercializes it. For a storage engine, DST + property-based testing + a reference model is the state of the art for correctness — directly transferable to a Rust storage engine.

## Recommendations

**A staged plan for building deep understanding, with concrete thresholds.**

1. **Establish the mental model (week 1).** Read Warfield's "Building and operating a pretty big storage system called S3" (All Things Distributed, 2023) and Vogels' "Diving Deep on S3 Consistency" (2021). Goal: internalize the four-plane architecture and the witness/cache-coherence mechanism. *Checkpoint*: you can explain why prefixes aren't directories and why strong consistency didn't cost latency.

2. **Get the durability/erasure-coding fundamentals (week 1–2).** Read the Backblaze Reed-Solomon + durability blog posts and skim `JavaReedSolomon`. Do the worked GF(2⁸) example by hand; implement RS(4,2) encode/decode in Rust over GF(256) with log/antilog tables. *Checkpoint*: you can reconstruct data after deleting 2 of 6 shards.

3. **Read the canonical papers, in this order** (weeks 2–5): **GFS (2003)** → **Haystack (OSDI 2010)** → **Azure Storage (SOSP 2011)** → **f4 (OSDI 2014)** → **Ceph/CRUSH (OSDI 2006)** → **Azure LRC (ATC 2012)** → **S3 ShardStore (SOSP 2021)**. The ShardStore paper is the capstone for you — it's Rust, it's a storage node, and it's about earning correctness. *Checkpoint*: you can compare S3's "store the map" vs. Ceph's "compute the map" and articulate the repair-bandwidth tradeoff LRC solves.

4. **Read Rust codebases, in this order** (ongoing): **`object_store`** crate (client abstraction — start here, small and idiomatic) → **Mountpoint for S3** (FUSE + CRT, production Rust) → **Garage** (full distributed store: CRDTs, consistent-hash ring, Merkle anti-entropy; multi-crate layering to emulate) → **`s3s`** (implement the S3 protocol over your own engine). If you want to *build*, `s3s` + your storage engine is the fastest path to an S3-compatible service.

5. **Adopt the correctness toolkit for your own engine (ongoing).** Add property-based tests (`proptest`), a reference model, and **Loom** for your concurrency primitives; study **FoundationDB**'s and **TigerBeetle**'s DST approach and consider Antithesis. This is the single highest-leverage practice transfer from S3 to your work.

6. **When evaluating "S3 as primary storage" for your own systems (broker/DB):** default to the diskless pattern **if** your workload is throughput-bound and latency-tolerant (≥ tens of ms acceptable) — you gain elasticity and lose replication cost. Use **S3 Express One Zone** if you need single-digit-ms and can co-locate compute in one AZ. Use **conditional writes (If-Match/If-None-Match)** as your commit/coordination primitive instead of an external lock manager. *Threshold to reconsider*: if p99 read latency must be < 5 ms or you need > ~100K writes/s to a single logical stream, revisit (local NVMe, or Express + sharding).

## Caveats

- **Scale numbers are dated and grow fast.** Cite the date: 100T objects / tens-of-millions req-s (2021) → hundreds of trillions (2025) → 500T objects / 200M req-s / 123 AZs / 39 regions (March 2026, official AWS). Don't state a single "current" number without its date.
- **"~350 microservices" is a third-party (ByteByteGo) figure, not official.** AWS/Warfield say "hundreds of microservices." Treat 350 as illustrative.
- **The internal keymap sharding scheme (range vs. hash) is not officially disclosed.** "Range-partitioned by key prefix" is the strongest well-supported statement (from re:Invent performance guidance + observed prefix-throughput behavior), not a published architecture spec. AWS's public term is "metadata/index subsystem," not the older internal codename "keymap."
- **Glacier's physical media is undisclosed.** HDD (spun-down/SMR) is the well-supported inference; tape/optical is speculation. Don't assert it as fact.
- **MinIO timeline** is assembled from community/vendor reporting (Blocks & Files, InfoQ, vendor blogs); the load-bearing facts (AGPL relicense 2021, console removal Feb 2025, maintenance mode Dec 3 2025, ~$96K/yr entry) are consistently reported but partly from parties (e.g., competitors) with an interest.
- **KIP-1150 is *accepted*, not *shipped*.** March 2, 2026 acceptance validates the direction; production-ready upstream Apache Kafka diskless is likely years away (KIP-500/KRaft took ~5.5 years as precedent). Use WarpStream/AutoMQ/Inkless today if you need it now.
- **Vendor performance claims** (S3 Tables "3×/10×", Express "10×", S3 Vectors "up to 90% cheaper", WarpStream "~24×") are first-party marketing figures — directionally credible, workload-dependent; benchmark for your case.
- **S3 Vectors trades latency for cost**: ~100 ms, not sub-ms. It complements, not replaces, hot vector DBs.
- **Some "how it works" writeups of R2/S3 internals** (e.g., R2-on-Durable-Objects details, exact CRT throughput) come from vendor docs and community posts; the high-level shapes are reliable, but exact internal mechanisms beyond official papers/blogs should be treated as informed inference.
