<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 12 — Distributed Transcoding Pipeline

> Project 11 turned one video file into a playable HLS/DASH stream — but it assumed
> the file was *already* at the right codec and bitrates. Real platforms don't get
> that gift: an upload arrives as one giant 4K ProRes (or a phone's H.265), and
> before it can be packaged it has to be **transcoded** into the whole ladder
> (1080p, 720p, 480p, …). Transcoding a two-hour movie serially on one machine
> takes *hours* — longer than the movie. The only way out is to go wide: cut the
> source into chunks, transcode the chunks in parallel across many workers, and
> stitch the results back together. That sounds like "just split the file", and it
> is a trap at every step. You can only cut at **keyframes**, or a chunk can't
> decode standalone. The work isn't a queue — it's a **DAG**: hundreds of chunk
> transcodes fan out from one split and fan back in to one stitch per rendition, and
> the stitch can't start until *every* chunk it needs is done, so one slow or dead
> worker stalls the whole join. Each chunk is encoded in its own process with its
> own timeline starting at zero, so gluing them back naively leaves a visible
> **seam** — a timestamp jump, a dropped frame, an audio pop — at every boundary.
> And because a worker can die mid-encode, tasks must be **idempotent**: a re-run
> has to reproduce the same bytes, or your "exactly once" output silently doubles a
> chunk. None of the interesting parts are a library call: you plan the cuts, you
> build and schedule the DAG, you run the transcodes in parallel and safely, and you
> stitch the pieces into one seamless file. This is `ffmpeg input.mov output.mp4`
> turned into a distributed system.

## What it does (the easy part)
- `POST /jobs` with `{ "source": "bbb.mp4", "ladder": [...] }` (ladder optional →
  server default) → `202 Accepted` with a job id. The source resolves under
  `WORK_DIR`; the ladder is the set of output renditions.
- `GET /jobs/{id}` → the job's status and **per-status task counts**
  (`{"done": 40, "running": 4, "ready": 12, "pending": 8, "failed": 0}`), so you
  can watch the DAG drain.
- `GET /healthz` is liveness.
- A pool of **workers** (same process or many processes/nodes) drains the DAG:
  split → parallel transcodes → stitch. Finished renditions land under
  `WORK_DIR/jobs/{id}/{rendition}/out.mp4` — ready to hand to project 11 to package.

> **Dependencies:** Postgres is the coordinator's durable memory — the job DAG
> (tasks + dependency edges + status + leases) lives in rows, so a crashed worker's
> task becomes claimable again and the pipeline survives a restart. The chunk and
> output **artifacts** live on the filesystem under `WORK_DIR`, not in the DB.
> `ffmpeg` / `ffprobe` are the external codec toolbox — the one thing you *don't*
> rebuild (an H.264 encoder is not the exercise). Everything *around* the encoder —
> where to cut, how to schedule, how to parallelize safely, how to stitch — is what
> you build. `docker compose up -d` brings up Postgres; `ffmpeg` must be on `PATH`.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Keyframe-aligned chunking — *decide where to cut*
In `src/chunk.rs`, turn the source's keyframe timestamps (from
`ffmpeg::probe_keyframes`, wired plumbing) and its duration into a set of chunks of
~`target_secs` whose boundaries are **all keyframes**. This is the "map" step's
plan: get it wrong and nothing downstream can be correct.

A decoder can only start at a **keyframe** (an IDR frame that references nothing
before it). Cut mid-GOP and the chunk's first frames reference frames that aren't
in the chunk — it can't decode standalone, and the re-encode produces garbage. So a
chunk boundary may fall *only* on a keyframe; the target duration is a goal, the
keyframe boundary is the law. This module is pure arithmetic over timestamps (no
media bytes), so it's exhaustively property-testable.

**Done when ALL true:**
- [ ] Every chunk boundary — each chunk's `start`, and every `end` but the last —
  is a **keyframe timestamp**: no cut ever falls off a keyframe.
- [ ] Chunks are **gapless and total**: chunk *n+1* starts exactly where chunk *n*
  ends, the first starts at `0.0`, the last ends at the source duration — every
  frame belongs to exactly one chunk, none to two.
- [ ] Chunk lengths **cluster around `target_secs`** but are allowed to exceed it
  when a single GOP is already longer — the keyframe boundary wins over the target,
  and that tradeoff is visible in the plan.
- [ ] Chunks are **indexed `0..n` in ascending time order** — so stitch order (V4)
  is just numeric order.
- [ ] **Degenerate inputs never panic:** a source with one keyframe (or none usable
  past the start), or a target larger than the whole asset, yields **one** valid
  chunk `[0.0, duration)`.

**Proof:** property tests over random ascending keyframe lists + durations asserting
boundaries-are-keyframes, gapless-and-total coverage, and no-panic
(`prop_chunks_are_keyframe_aligned`, `prop_chunks_cover_source`); `docs/12-design.md`
records the target-vs-keyframe policy.

*Concept to internalize:* GOP structure and why only IDR/keyframe boundaries yield
independently-decodable chunks; open vs closed GOPs; the split/transcode/stitch
("map/reduce for video") shape and why the cut points are load-bearing.

### V2. The job DAG + scheduler — *model the work as a graph*
In `src/dag.rs` (plus the durable store in `src/job.rs`), express a job as a
**dependency graph** and schedule it. A transcode job isn't a flat queue: one
`Split` fans out into one `Transcode` per (chunk × rendition), and each rendition's
`Stitch` fans those back in. Progress must flow along the edges — a task runs only
once its upstream tasks are done.

The graph is **discovered dynamically**: the `Split` task (run by a worker) learns
the chunk count from V1, then `dag::expand` builds the transcode + stitch tasks and
their edges, which the store persists. The scheduler's core is `dag::newly_ready`
(pure, in-memory) mirrored by `JobStore::promote_ready` (its SQL twin): given the
task states, which `Pending` tasks now have **all** dependencies `Done`?

**Done when ALL true:**
- [ ] The DAG has the right **shape**: every `Transcode` depends on the job's
  `Split`; every rendition's `Stitch` depends on **all** of that rendition's
  `Transcode` tasks (the fan-in) and no others.
- [ ] A task becomes **runnable exactly when all its dependencies are `Done`** — never
  before (no transcode starts before the split; no stitch starts before its last
  chunk), and every task with satisfied deps *does* become runnable (no deadlock).
- [ ] The DAG is **durable**: kill and restart the whole coordinator mid-job and it
  resumes from the persisted task states — finished tasks are not redone, unfinished
  ones still run. No task list lives only in memory.
- [ ] A job's **status is derived from its tasks**: it is `done` only when every task
  is `Done`, and `failed` if any task is `Failed` — and `GET /jobs/{id}` reports
  accurate per-status counts throughout.
- [ ] The graph is acyclic and **terminates**: for any job, running ready tasks to
  completion eventually drains every task to `Done` (or a `Failed` that's explained).

**Proof:** unit tests on `dag::expand` asserting the edge shape for a 2-rendition,
N-chunk job (`expand_wires_fan_in`), and on `dag::newly_ready` that a stitch is
withheld until its last chunk flips `Done` (`stitch_waits_for_all_chunks`); an
integration test that restarts the store mid-job and shows no task is re-run
(`dag_resumes_after_restart`); `docs/12-design.md` diagrams the DAG.

*Concept to internalize:* DAG scheduling (topological progress, ready-set,
fan-out/fan-in); why a dependency graph beats a flat queue for multi-stage work;
and why join nodes (`Stitch`) are where stragglers and deadlocks hide.

### V3. Parallel transcode workers — *run the chunks, idempotently*
In `src/worker.rs`, make the fan-out real: a pool of workers each claim a `Ready`
task, run it, and settle it, so dozens of chunk transcodes run at once. The loop is
wired; the crux is the **`Transcode`** handler and doing it **safely under
at-least-once**.

Because a lease can expire and a task re-run (a worker died, or was just slow), every
task must be **idempotent**: a re-run reproduces the *same* chunk bytes and commits
them atomically (write-temp-then-rename), so a duplicate run is harmless and a
half-written file is never mistaken for a finished one. The claim itself is the same
`FOR UPDATE SKIP LOCKED` lease you built in project 04 — reused, not re-taught; the
new learning is idempotent execution + the parallelism/backpressure of a real worker
pool.

**Done when ALL true:**
- [ ] A `Transcode` task transcodes **exactly one chunk** at one rendition — cut to
  the chunk's `[start, end)` and encoded to that rung — never the whole asset.
- [ ] Running **N workers gives ~N-way parallelism**: with independent chunks ready,
  N workers are busy at once and wall-clock time drops with N (measured, not assumed).
- [ ] Execution is **idempotent**: transcoding the same chunk twice yields the **same
  bytes**, and the artifact is committed **atomically** — an interrupted attempt
  leaves no partial file that a later step could mistake for done.
- [ ] A **worker that dies mid-task loses nothing**: its lease expires, the reaper
  returns the task to `Ready`, another worker completes it, and the final output is
  correct (no duplicated or missing chunk).
- [ ] A task that fails is **retried with backoff up to a limit, then dead-lettered**
  — a permanently-bad chunk fails its job cleanly instead of looping forever.

**Proof:** an integration test that submits a job, runs a worker pool, kills a worker
mid-transcode, and asserts the job still completes with every chunk present exactly
once (`killed_worker_is_recovered`); a determinism test that a chunk transcoded twice
is byte-identical (`transcode_is_deterministic`); a `bench/` run showing wall-clock
speedup vs. worker count in `docs/12-benchmarks.md`.

*Concept to internalize:* at-least-once vs exactly-once and why idempotency +
atomic commit bridge them; leases/visibility-timeout for crash recovery; worker-pool
parallelism, work-stealing via the shared claim, and backpressure (bounded
concurrency so you don't fork-bomb ffmpeg).

### V4. Stitch + remux — *glue the chunks back seamlessly*
In `src/stitch.rs`, concatenate one rendition's transcoded chunks into a single
continuous file — the "reduce" that joins the fan-out. Each chunk was encoded on its
own worker with its own timeline starting at zero; joining them naively produces a
**seam** at every boundary. Because boundaries are keyframe-aligned (V1), this is a
**remux** (rewrap + rebase timestamps), not a re-encode.

**Done when ALL true:**
- [ ] Chunks are joined in **numeric index order** — `10.mp4` follows `9.mp4`, never
  sorted lexicographically (which would put `10` before `2`).
- [ ] The output's **presentation timestamps are monotonic and gapless across every
  boundary**: no backwards jump, no gap, no overlap where two chunks meet.
- [ ] The stitched output's **total duration equals the summed chunk durations
  within one frame** — no rounding drift accumulates across a long asset.
- [ ] **A/V stays in sync** across boundaries — audio and video don't drift apart at
  the seams.
- [ ] Stitching is a **remux, not a re-encode** (no extra generation of quality loss),
  and is **idempotent + atomic** (temp→rename) so a re-run after a crash is safe and
  never publishes a partial file.

**Proof:** an integration test feeding real chunks through `stitch` and asserting via
`ffprobe` that PTS are monotonic/gapless and total duration matches within a frame
(`stitched_output_has_no_seam`, `stitched_duration_matches_source`); a numeric-order
test (`chunks_ordered_numerically`); `docs/12-design.md` records the concat/remux
method and the timestamp-rebasing rule.

*Concept to internalize:* why independently-encoded chunks have discontinuous
timelines and how `baseMediaDecodeTime`/PTS rebasing removes the seam; remux vs
re-encode; and why the whole scheme only works because V1 cut on keyframes.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols / API
- [ ] `POST /jobs` returns **`202 Accepted`** (the work is async, not done when the
  call returns) with the job id; `GET /jobs/{id}` reports live status + task counts;
  an unknown id is a clean **`404`**.
- [ ] **Content types** are correct (`application/json` on the API) and the job view
  is stable, documented JSON a dashboard can poll.
- [ ] **Graceful shutdown**: on SIGTERM the coordinator stops *claiming* first, then
  lets in-flight transcodes finish or their leases expire — never aborts a task and
  loses its ack, and drains in-flight HTTP requests.

### Caching / reuse
- [ ] Finished chunk artifacts are **memoized**: a task whose output already exists
  (from a prior attempt) is not re-transcoded — detectable by the atomic-commit
  marker, so at-least-once re-runs are cheap.
- [ ] Chunk outputs are **content-stable** (deterministic bytes), so an artifact cache
  or CDN in front of `WORK_DIR` stays coherent — the reuse contract V3 depends on.

### Security / abuse protection
- [ ] **Path traversal is impossible** (`PipelineConfig::resolve_source`): a client
  `source` can never escape `WORK_DIR` (`../`, absolute paths, symlinks); a bad path
  is a clean `400`/`404`, never a filesystem probe or a 500.
- [ ] **No shell injection into ffmpeg**: arguments are passed as an argv vector, never
  interpolated into a shell string — a source name with spaces/`;`/quotes can't run
  commands. Inputs (ladder height/bitrate, names) are **validated and bounded**.
- [ ] `POST /jobs` is **authenticated** (an open submit lets anyone make your workers
  execute ffmpeg on arbitrary inputs — an obvious DoS/abuse vector), and resource
  ceilings are noted (max ladder rungs, max concurrent jobs).

### Observability
- [ ] A `tracing` span per request and per task (via `common-telemetry`) carrying
  `job_id`, `task_id`, and `kind` (chunk index + rendition) — so one chunk's journey
  is traceable. Never log source paths at info level or ffmpeg's full stderr except
  on error.
- [ ] Counters: jobs submitted, tasks by kind + outcome (done/retried/dead-lettered),
  **leases reclaimed** (dead-worker recoveries), and chunk **cache hits** (skipped
  re-transcodes).
- [ ] Histograms/gauges: **per-chunk transcode time**, DAG **queue depth**
  (ready/running), and **worker utilization** — enough to see a straggler forming.

---

## Cross-cutting scale skills
- **Fan-out / fan-in:** the map/reduce shape for media — wide parallel transcodes,
  narrow joins — and the DAG that expresses it durably.
- **Idempotency under at-least-once:** deterministic outputs + atomic commit turn
  "a task might run twice" from a corruption bug into a no-op.
- **Backpressure:** bounded worker concurrency so a flood of ready chunks doesn't
  fork-bomb ffmpeg and thrash the box — the pool size *is* the throttle.
- **Crash recovery:** leases + a reaper mean no single worker's death strands a
  chunk; the fan-in still completes.
- **Bounded memory/disk:** work a chunk at a time; a 4 GB source never lives in RAM,
  and finished chunks can be reaped after the stitch.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load/failure test lives in `bench/`,
   the numbers in `docs/12-benchmarks.md`.
3. `docs/12-design.md` records the decisions the SPEC grades: the **keyframe-chunking
   policy** (target vs boundary), the **DAG model** (node/edge shape, dynamic
   expansion, readiness rule), the **idempotency + lease/recovery** design, and the
   **stitch/remux + timestamp-rebasing** method.
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p transcode-pipeline`
   are green; no `todo!()` remains on a checked path.

## 🐉 Boss fight — The Straggler

> A feature-length upload lands and explodes into hundreds of chunk-transcode tasks
> fanning out across your worker pool. Everything's flying — until one worker dies
> mid-encode holding a single chunk. The rendition's `Stitch` is a fan-in: it can't
> start until **every** chunk is done, so that one lost chunk now threatens to stall
> the entire job behind it. Meanwhile you're being graded on wall-clock: a serial
> transcode would take forever, and if your parallelism sags or the straggler isn't
> recovered fast, you blow the deadline — and if your stitch has a seam, none of the
> speed mattered because the output is broken. Defeat the Straggler and you've proven
> the whole pipeline: wide, recoverable, and seamless.

**Arena:** `bench/` runs a **release build** (`cargo run --release`) with Postgres up
and a real multi-minute source under `WORK_DIR`. Submit one job over a 3-rendition
ladder; run a worker pool; partway through, **`kill -9` one worker**. Compare against
a serial baseline (1 worker) and an un-killed run.

**The boss falls when ALL true:**
- [ ] **Parallel speedup ≥ 6× at 8 workers** vs. the 1-worker serial baseline on the
  same source+ladder (≥ ~75% efficiency) — measured wall-clock, end to end.
- [ ] **Faster than realtime:** the job's aggregate throughput exceeds the source's
  playback duration (encodes ≥ 1× realtime overall across the ladder; note the
  factor achieved).
- [ ] **The straggler is recovered:** after a worker is `kill -9`'d mid-transcode, the
  job still **completes**, with **every chunk present exactly once** (no missing, no
  duplicate), and the killed run finishes within **1.5×** the un-killed wall-clock.
- [ ] **Seamless output:** for every rendition, `ffprobe` shows **monotonic, gapless
  PTS across all boundaries** and total duration within **one frame** of the source —
  zero seams.
- [ ] **No re-transcode waste:** the recovery re-runs **only** the dead worker's
  in-flight chunk, not completed ones (prove it with the cache-hit / task-attempt
  counters, not vibes).

**Proof:** methodology + speedup-vs-workers table + the kill-recovery timeline in
`docs/12-benchmarks.md` (hardware + source noted, commands reproducible via `bench/`).

## Suggested order of attack
1. Get the boring path working: `POST /jobs` inserts a job + a seed `Split` task;
   `GET /jobs/{id}` reports task counts; `GET /healthz` is green — no workers yet.
2. Build V1: the keyframe chunk planner, pure — property-test boundaries-are-keyframes
   and gapless coverage before touching ffmpeg.
3. Build V2: `dag::expand` (the fan-out/fan-in shape) + the store (persist tasks/edges)
   + `promote_ready`/`newly_ready` (the readiness rule); unit-test the DAG shape.
4. Build V3: the claim/lease + one `Transcode` handler that deterministically encodes
   one chunk and commits atomically; turn on `RUN_WORKERS`, run a few, watch the DAG
   drain; add retries + the reaper.
5. Build V4: order chunks numerically and stitch/remux into one seamless output;
   validate PTS continuity + duration with `ffprobe`.
6. Add auth + the traversal guard + argv-safety + metrics; then benchmark the
   speedup, kill a worker, and document — hand the finished renditions to project 11
   to package.

## Run the dependencies
```bash
docker compose up -d        # postgres
cp .env.example .env        # set WORK_DIR + DATABASE_URL; ensure ffmpeg is on PATH
sqlx migrate run            # apply migrations (install: cargo install sqlx-cli)
# Drop a source under $WORK_DIR (e.g. work/bbb.mp4), then:
cargo run -p transcode-pipeline
#   The scaffold compiles and serves the control-plane API. `POST /jobs` hits a
#   todo!() in V2; flipping RUN_WORKERS=true makes the scheduler/workers panic on
#   the first store call — those panics are the worklist.
```
