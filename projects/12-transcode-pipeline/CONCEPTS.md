# Concept Bank — Project 12: Distributed Transcoding Pipeline

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — Keyframe-aligned chunking: deciding where to cut *(V1 · `src/chunk.rs`)*

**The problem.** Transcoding a two-hour movie serially takes longer than the movie. The only way out is parallelism: cut the source into chunks, encode them on many workers at once. But video isn't a byte stream you can slice anywhere — most frames are *deltas* that reference earlier frames. Cut mid-GOP and the chunk's first frames point at data the chunk doesn't contain: it can't decode standalone, and the re-encode produces garbage that only shows up as visual corruption after hours of compute.

**The idea.** A decoder can only start at an IDR keyframe (references nothing before it). So the cut *plan* is pure arithmetic over the source's keyframe timestamps: boundaries fall only on keyframes; chunks are gapless and total (every frame in exactly one chunk); the duration target is a preference, the keyframe boundary is the law — a 20-second GOP produces a 20-second chunk, and that's correct. Because it's pure math over timestamps, it's exhaustively property-testable before any media is touched.

**In the wild:** every cloud transcoder (AWS MediaConvert, YouTube's ingestion, Mux) does split-encode-stitch; "map/reduce for video" is the standard description.

**You own it when you can explain:**
- [ ] GOP structure — I/P/B frames, what each references — and why only IDR frames are valid entry points.
- [ ] The corruption mechanism if a cut misses a keyframe (dangling references → decoder garbage), and why it's silent until you look at pixels.
- [ ] The plan invariants: boundaries-are-keyframes, gapless-and-total, ascending index order — and why each one is load-bearing for the stitch (V4).
- [ ] Open vs closed GOPs — why an open GOP (frames referencing *across* a keyframe) complicates "the keyframe boundary is safe".
- [ ] Why degenerate inputs (one keyframe, target > duration) must collapse to one valid chunk rather than panic.

**Depth probes:**
- Why is chunking pure arithmetic a *design win* — what does keeping media I/O out of the planner buy for testing and for the scheduler?
- The source has keyframes every 2 s but your target is 30 s. What does the plan look like, and does chunk-count vs chunk-size matter for the pool?

**Trap:** validating the plan by encoding it. Property-test the arithmetic first — an encode-based test takes minutes and tells you *that* it broke, not *which invariant* broke.

---

## 🧠 Card 2 — Work as a DAG, not a queue *(V2 · `src/dag.rs`, `src/job.rs`)*

**The problem.** This workload has *shape*: one Split fans out into (chunks × renditions) Transcodes, which fan back into one Stitch per rendition — and a stitch must not start until its *last* chunk finishes. A flat queue can't express "not until all of these are done"; hand-rolled flags and counters can, badly, until a restart wipes them and the job is stranded half-finished with no record of what ran.

**The idea.** Model the job as an explicit dependency graph in durable storage: tasks as rows, edges as rows, states as columns. The scheduler's core is one pure question — *which pending tasks now have all dependencies done?* — asked after every completion. The graph is discovered dynamically (chunk count is known only after the Split runs, so the Split's completion *expands* the graph). Job status is derived from task states, never stored separately (a projection, not a second truth). Restart-safety falls out: the DAG is in Postgres, so a coordinator crash resumes exactly where it stopped.

**In the wild:** Airflow/Dagster/Temporal model work exactly this way; CI systems (GitHub Actions' `needs:`) are DAG schedulers; Spark/MapReduce stage scheduling is the same fan-out/fan-in.

**You own it when you can explain:**
- [ ] Why fan-in is the thing a queue can't express, and where deadlock hides (an edge that never satisfies) vs starvation (a ready task never promoted).
- [ ] The readiness rule as a pure function of task states — and why keeping it pure (mirrored by its SQL twin) makes it unit-testable.
- [ ] Dynamic expansion: why the graph can't be prebuilt, and what atomicity the expansion step needs (all tasks+edges land, or none).
- [ ] Status-as-projection: why `done iff every task done, failed if any failed` beats a stored status column that can drift.
- [ ] Why join nodes are where stragglers concentrate: the stitch waits on the *max* of its inputs, so one slow chunk delays everything behind it.

**Depth probes:**
- Amdahl's law on this DAG: with serial Split and Stitch, what bounds the speedup at 8, 64, 512 workers?
- A rendition's Stitch fails permanently. What should happen to the sibling renditions of the same job — and what does your status derivation say?

**Trap:** encoding dependencies implicitly ("workers just check if the chunks exist"). It works until two workers race the check, or a restart forgets which stitch already ran — the explicit durable edge is what makes the invariant checkable.

---

## 🧠 Card 3 — Idempotent execution under at-least-once *(V3 · `src/worker.rs`)*

**The problem.** Leases (project 04's pattern, reused here) guarantee a dead worker's task gets re-run — which means every task *will sometimes run twice*: the killed worker's half-finished output is on disk when the retry starts, or a slow worker's lease expires and two workers run the same chunk *simultaneously*. If a re-run appends, doubles, or observes the half-written file as finished, recovery corrupts the very output it was saving.

**The idea.** Make every task a deterministic, atomically-committed function: same chunk in → same bytes out, written to a temp path and `rename`d into place (project 06's commit, again). Then a duplicate run is a no-op (it produces the identical artifact), a half-written file is invisible (never at the final path), and memoization falls out free: output already exists → skip the work. At-least-once delivery + idempotent execution = effectively-exactly-once results. Bounded worker concurrency is the backpressure: the pool size is the throttle that keeps a flood of ready chunks from fork-bombing ffmpeg.

**In the wild:** build systems (Bazel's hermetic actions + content-addressed cache) are exactly this; every serious data pipeline (Spark task retries) assumes idempotent tasks; Temporal activities (project 21) formalize it.

**You own it when you can explain:**
- [ ] The two duplicate-run scenarios (crash-and-retry, slow-worker-plus-lease-expiry racing) and how determinism + atomic commit makes both harmless.
- [ ] Why *both* halves are needed: determinism without atomic commit still exposes partial files; atomic commit without determinism makes concurrent runs disagree.
- [ ] Memoization as a consequence, not a feature: why "output exists = done" is only sound because of the atomic-commit marker.
- [ ] What encoder nondeterminism (threads, timestamps embedded in output) would break, and why the SPEC demands byte-identical re-runs.
- [ ] The pool size as backpressure: what unbounded ffmpeg spawning does to a box (memory, CPU thrash, everything slower than serial).

**Depth probes:**
- Speedup measured at 6× on 8 workers. Walk the missing 2×: serial stages, claim contention, straggler tails, I/O bandwidth.
- Recovery re-ran *only* the dead worker's chunk. Which two mechanisms combined prove that (memoized artifacts + task-attempt counters)?

**Trap:** writing output directly to the final path "since we'll overwrite on retry anyway". The stitch can start the moment the last task *acks* — a file that exists-but-is-partial at that instant is the corruption you can't detect cheaply.

---

## 🧠 Card 4 — Stitching: the seamless join *(V4 · `src/stitch.rs`)*

**The problem.** Each chunk was encoded by its own ffmpeg process with its own timeline starting at zero. Concatenate them naively and every boundary has a *seam*: presentation timestamps jump backwards to 0, players stutter or resync, audio pops. And a sort bug you'd never catch in a 9-chunk test — lexicographic ordering putting `10.mp4` before `2.mp4` — scrambles the movie at 10+ chunks.

**The idea.** The stitch is a **remux**, not a re-encode: because cuts were keyframe-aligned (V1), the encoded frames are already valid — only their container timestamps need rebasing. Offset each chunk's PTS by the running sum of prior durations so the output timeline is monotonic and gapless; join in numeric index order; verify with ffprobe-style checks (monotonic PTS, total duration within one frame of the source, A/V sync held at boundaries). Same idempotency + atomic commit discipline as V3 — a crashed stitch must never publish a partial file.

**In the wild:** every split-encode pipeline ends in exactly this remux-concat; ffmpeg's concat demuxer does the same timestamp rebasing; project 13 does the same rebasing *live*.

**You own it when you can explain:**
- [ ] Why the seams exist at all (independent zero-based encode timelines) and what rebasing computes for chunk N.
- [ ] Remux vs re-encode: what a re-encode stitch would cost (a full extra generation of quality loss + the serial encode time you parallelized to avoid).
- [ ] Why the whole scheme is downstream of V1: misaligned cuts make a seamless remux impossible no matter how good the stitch is.
- [ ] The verification story: which ffprobe-observable properties (monotonic gapless PTS, duration-within-a-frame, A/V sync) together mean "no seam".
- [ ] Why rounding drift matters at feature length: a per-boundary error of 1 ms is 300 ms of A/V drift after 300 chunks — where does your rebasing accumulate error, or not?

**Depth probes:**
- Audio frames and video frames don't share boundaries (1024-sample AAC frames vs video GOPs). What does that do to "cut on keyframes" and to A/V sync at chunk edges?
- Could you stream the stitch (start serving while later chunks still encode)? What ordering/availability constraints appear?

**Trap:** validating the stitch by watching it. A half-second seam at minute 37 of one rendition won't be watched — the ffprobe invariants are the test; eyes are the fallback.

---

## ⚡ Rapid-fire round

- [ ] `202 Accepted` + a pollable job resource — the async-API contract for long-running work.
- [ ] Argv arrays, never shell strings, for ffmpeg — the injection a filename with `;` achieves otherwise.
- [ ] Path traversal on `source` — a job payload must never address files outside `WORK_DIR`.
- [ ] Why `POST /jobs` needs auth: it's "run expensive compute on demand" — a free cryptominer endpoint otherwise.
- [ ] The observability that finds a straggler: per-chunk transcode-time histogram + ready/running queue depths + worker utilization.
- [ ] Graceful shutdown: stop claiming, let in-flight encodes finish or lapse — the lease makes even an ungraceful death recoverable.

## 🔗 Connects to

- The claim/lease/reaper machinery is project 04, reused as promised — the *new* lesson is idempotent execution on top of it.
- The atomic temp→rename commit is project 06's discipline applied to artifacts.
- The output ladder feeds project 11's packager; the whole pipeline becomes the transcode plane of project 16.
