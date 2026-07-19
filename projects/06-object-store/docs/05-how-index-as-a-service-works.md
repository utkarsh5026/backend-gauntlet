# How Index-as-a-Service Works — From First Principles

> A beginner-friendly, ground-up guide to the idea behind the SPEC's
> "index microservice" lab: running the `(bucket, key) → blob` map in a
> **separate process** from the S3 front-end, the way large object stores
> separate metadata from storage nodes. No prior distributed-systems experience
> assumed.
>
> This teaches the **concept**. It does **not** implement the From-the-field
> backlog item — that stays yours if you adopt it. The scaffold is stubs
> (`todo!()`) plus an in-process default that still works today.
>
> Anchored to: [`src/index.rs`](../src/index.rs),
> [`src/index_backend.rs`](../src/index_backend.rs),
> [`src/index_server.rs`](../src/index_server.rs),
> [`src/bin/object_store_index.rs`](../src/bin/object_store_index.rs),
> industry notes in [`RESEARCH.md` §Part 2](../RESEARCH.md), and the optional
> SPEC line under **From the field**.

---

## 0. The one sentence to hold onto

**The index is a different *role* from the blob store — at S3 scale that role
becomes a different *fleet*; in this project it starts as a different *binary*
you can opt into.**

Same contract (`put` / `get` / `list` / …). Different address space. The learning
is what a process boundary does to latency, failure, and the blob-then-pointer
invariant — not Kubernetes.

---

## 1. What RESEARCH is describing

S3's internal shape (Warfield / Vogels vocabulary):

```
Client → front-end fleet → index / metadata subsystem → storage-node fleet
                              ↑
                         this lab
```

The index maps keys to physical locations. It is on the GET/PUT/DELETE path,
range-partitioned, strongly consistent (after the 2020 witness/cache-coherence
work), and scaled independently of the disks that hold bytes.

AWS did **not** put that map in the HTTP front-end process because:

| Pressure | Why a separate service |
| --- | --- |
| Scale | Trillions of keys; metadata QPS ≠ byte throughput |
| Failure isolation | Metadata crash ≠ lose disks; storage crash ≠ lose the namespace |
| Ownership | Different teams, deploy cadence, SLOs ("ship the org chart") |
| Coherence | A shared cache + witness needs a single metadata plane |

Your graded V3 work already built the *role* (`Index` + crash-safe pointer
updates). This lab asks: what if that role crossed a network hop?

---

## 2. Same process vs RPC — what actually changes

**Default (`INDEX_URL` unset):**

```
one binary (object-store)
├── routes / S3 API
├── IndexBackend::Local(Index)   ← function call
└── Arc<Store>                   ← local FS blobs
```

**Split mode (`INDEX_URL=http://127.0.0.1:9106`):**

```
object-store (front-end)              object-store-index
├── routes / S3 API                   ├── /v1/... HTTP JSON
├── Store (blobs on DATA_DIR)         └── Index (DATA_DIR/index)
└── IndexBackend::Remote ──HTTP──►
```

| Concern | In-process | Over HTTP |
| --- | --- | --- |
| Latency | nanoseconds–µs | milliseconds + serialization |
| Partial failure | process dies together | index up / store down (or reverse) |
| API contract | Rust types | JSON + status codes + timeouts |
| Blob-then-pointer | crash between commit and index write | **also** "blob committed, RPC failed" |

That last row is the distributed version of V3's crash window: an unreferenced
blob (GC fodder) is still the survivable half-state; a key pointing at missing
bytes is not. The network does not invent a new rule — it makes the old rule
happen more often.

---

## 3. How the scaffold maps to your code

| Piece | Path | Status |
| --- | --- | --- |
| In-process index (graded V3) | `src/index.rs` | Real implementation |
| Backend enum + DTOs + remote client | `src/index_backend.rs` | `Local` delegates; `RemoteIndex` is implemented |
| Index HTTP router | `src/index_server.rs` | Handlers call `Index` and return JSON |
| Index binary | `src/bin/object_store_index.rs` | Binds `:INDEX_PORT` (default **9106**) |
| S3 front-end | `src/main.rs` + `AppState` | `Arc<IndexBackend>`; `INDEX_URL` selects Local/Remote |

Wire routes (internal, not S3 path-style):

- `PUT/HEAD /v1/buckets/{bucket}`, `GET /v1/buckets`
- `PUT/GET/DELETE /v1/buckets/{bucket}/keys/{*key}`
- `POST …/resolve`, `GET …/list`, `GET …/entries`, `POST /v1/gc`

Blobs stay on the front-end's `Store`. The index service only stores pointers
(and needs a `Store` handle for GC reclaim — same as today's `Index::open`).

`ensure_bucket` over the wire returns success/failure only — no `PathBuf`.
Bucket `metadata.json` I/O that currently uses the local path is an adoption
detail you must redesign when the front-end no longer shares the index disk.

---

## 4. Adoption checklist (your work)

Do this only after V3 is solid and you want the distributed lesson.

1. **Container stack (preferred)** — three services on a compose network:
   ```bash
   make stack
   # console http://localhost:5106  →  object-store:9000  →  index:9106
   # shared volume storedata at /data
   ```
2. **Or two host processes** sharing one `DATA_DIR`:
   ```bash
   make index-svc
   INDEX_URL=http://127.0.0.1:9106 make backend
   ```
3. **Chaos check** — `docker compose stop index` (or kill the index process)
   mid-PUT after the blob commit; assert no dangling key is served; orphan
   blobs are GC-able. That is the boss fight for this lab.

Flip the SPEC `[~]` to `[✔]` once you've proven the split end-to-end (and ideally
the chaos check).

**You do not need** Kubernetes or a service mesh. The arena is either two host
processes or `make stack` (Docker Compose: index + API + web).

---

## 5. Stretch (optional, later)

S3's strong consistency story added a **witness** so metadata caches could not
serve stale views after a write. Once Local/Remote works, a tiny in-front-end
cache of `ObjectMeta` plus a generation / "notify on write" check is the
miniature version of that idea. Skip it until the RPC path is boring.

---

## 6. Common traps

| Trap | Reality |
| --- | --- |
| "Microservices = more correct." | They teach failure modes; they do not fix a wrong blob-then-pointer order. |
| Sharing one `DATA_DIR` without thinking. | Front-end owns `objects/`; index owns `index/`. GC on the index side still deletes blobs — both processes need a coherent view of `objects/`. |
| Implementing Remote before Local delegates. | Keep `IndexBackend::Local` as the regression path; tests can stay in-process. |
| Treating this as graded V3. | V3 is the durable index *logic*. This lab is deployment topology. |

---

## 7. Where this sits in the SPEC

| Graded | From the field |
| --- | --- |
| V3 crash-safe index, listing, GC | Architecture lab: index as a second process |
| Definition of done / boss fight | Uncounted `[~]` / `[✔]` checkbox |

Teach-yourself only. The default `make dev` path stays a single binary until
you opt in.
