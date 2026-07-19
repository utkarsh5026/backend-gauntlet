# The Fundamentals Woven Through This Project

> The horizontal checklist, taught. Six backend fundamentals that aren't a
> vertical of their own but are graded all the same: the async-API contract,
> shell-injection safety, path traversal, why the submit endpoint needs auth,
> the observability that catches a straggler, and graceful shutdown. Each maps
> to a checklist item in [SPEC.md](../SPEC.md) and to real code in this
> scaffold. No prior knowledge assumed.
>
> Anchored to: [src/routes.rs](../src/routes.rs), [src/job.rs](../src/job.rs),
> [src/ffmpeg.rs](../src/ffmpeg.rs), [src/worker.rs](../src/worker.rs),
> [src/main.rs](../src/main.rs).

---

## 1. `202 Accepted` — the async-API contract

A transcode takes minutes to hours. HTTP callers wait milliseconds. The contract
for long-running work is therefore *not* "do the work in the handler":

```
POST /jobs            ─▶ 202 Accepted { "id": … }     "I have RECORDED your request"
GET  /jobs/{id}       ─▶ 200 { status, tasks: {…} }    poll me
GET  /jobs/{unknown}  ─▶ 404                           clean, not a 500
```

`202` means exactly one thing: *accepted for processing, not done* — the
opposite of `201 Created` (the resource exists now). What makes `202` honest is
the **pollable resource** that comes with it: the job id plus
`GET /jobs/{id}`'s per-status task counts ([`TaskCounts`](../src/job.rs)) let a
dashboard literally watch the DAG drain. The wired handler in
[`routes.rs`](../src/routes.rs) already returns `StatusCode::ACCEPTED`; what
makes it *true* is your V2 `submit` recording the job durably before the 202
leaves the building.

The checklist also demands the JSON view be **stable and documented** — a
polling contract is an API surface; renaming `"done"` to `"complete"` breaks
every consumer silently.

## 2. Argv arrays, never shell strings

Your workers shell out to ffmpeg with client-influenced values (the source
name). There are two ways to launch a process, and they are not equally safe:

```
the injection:   source name =  bbb.mp4; rm -rf "$WORK_DIR"

shell string:    sh -c "ffmpeg -i bbb.mp4; rm -rf "$WORK_DIR" …"
                                        ▲ the ; ends ffmpeg's command — the rest RUNS

argv vector:     execve("ffmpeg", ["-i", "bbb.mp4; rm -rf \"$WORK_DIR\"", …])
                                   ▲ one inert argument; no shell ever parses it
```

A shell *interprets* metacharacters (`;`, `|`, `$()`, quotes); an argv vector is
handed to the kernel as inert strings. The scaffold already funnels every
invocation through [`ffmpeg::run(bin, &[String])`](../src/ffmpeg.rs) —
`tokio::process::Command` with `.args(...)`, no shell anywhere. Your job in V3/V4
is to keep it that way: *build vectors, never format command strings*. The
checklist's second half — validate and bound the inputs (ladder height/bitrate
ranges, name shapes) — is defense in depth on top.

## 3. Path traversal: `source` must not escape `WORK_DIR`

`POST /jobs` takes a `source` path and workers open it. Unchecked, that's a
read-anything primitive:

| Client sends | Naive `work_dir.join(source)` resolves to |
| --- | --- |
| `bbb.mp4` | `WORK_DIR/bbb.mp4` ✓ |
| `../../etc/passwd` | `/etc/passwd` — `join` happily walks up |
| `/etc/shadow` | `/etc/shadow` — joining an *absolute* path **replaces** the base |
| `link.mp4 → /etc/passwd` | a symlink laundering the same escape past a string check |

Two non-obvious rules there: `PathBuf::join` with an absolute path *discards*
the base entirely, and string inspection can't see symlinks — only resolving
the real path can. The guard lives in
[`PipelineConfig::resolve_source`](../src/job.rs) (a `todo!()` stub, so the
wiring type-checks): resolve the candidate to its canonical real path and reject
anything not under `work_dir`. Failure mode matters too — a bad path is a clean
`400`/`404`, never a 500 and never an error message that echoes what the probe
found (that turns your API into a filesystem oracle).

## 4. Why `POST /jobs` needs auth

The submit endpoint is, functionally, **"run heavy compute on my hardware,
free"**. Left open on a network:

- anyone drives your ffmpeg pool at 100% forever — a denial of service against
  every legitimate job (the fan-out multiplies each submit into hundreds of
  tasks: one 2-hour source at 6-second chunks × 3 renditions = 3,600 encodes);
- combined with a traversal hole it becomes "transcode `/etc/…` and let me poll
  the error"; combined with unbounded ladders (100 rungs at 8K?) it's a
  resource-exhaustion kit.

The checklist asks for authentication on submit (the same pattern project 04
used for enqueue) plus **stated ceilings**: max ladder rungs, bounded
height/bitrate, max concurrent jobs. Note what auth is *not* needed for:
`GET /healthz` must stay open for the orchestrator's liveness probe.

## 5. Observability: seeing the straggler form

The boss fight kills a worker mid-encode and asks you to *prove* recovery with
counters, not vibes. Work backwards from the questions to the instruments:

| Question during the fight | Instrument (checklist item) |
| --- | --- |
| "Is one chunk taking 10× the others?" | **Histogram**: per-chunk transcode seconds — a straggler is a fat right tail |
| "Are workers starving or drowning?" | **Gauges**: ready vs running queue depth, worker utilization |
| "Did the dead worker's task get rescued?" | **Counter**: leases reclaimed — the reaper's scoreboard |
| "Did recovery redo finished work?" | **Counters**: task attempts + chunk cache hits — "no re-transcode waste" is *these two numbers* |
| "What happened to chunk 17, rendition 720p?" | **Span per task** carrying `job_id`, `task_id`, `kind` — one chunk's whole journey, greppable |

The span plumbing comes from `common-telemetry` (see
[`main.rs`](../src/main.rs)); the discipline is *cardinality and hygiene*: label
by kind and outcome, never by unbounded values, and — per the checklist — never
log source paths at info level or ffmpeg's full stderr except on error (stderr
can embed the full command line, paths included).

## 6. Graceful shutdown: stop claiming, then drain

SIGTERM arrives (a deploy, a scale-down). The wrong response is `exit(0)` with
eight encodes in flight. The right sequence is already shaped by the scaffold's
`watch::Receiver<bool>` shutdown channel threaded through
[`Worker::run`](../src/worker.rs) and [`schedule_loop`](../src/dag.rs):

```
SIGTERM
  1. stop CLAIMING new tasks        (workers check the flag before each claim)
  2. stop accepting new HTTP work; drain in-flight requests
  3. let in-flight encodes FINISH and ack (complete/fail settles them)
     …or, if the deadline passes: die anyway — the LEASE has you covered
```

Step 3 is the insight the rapid-fire card points at: because V3's leases +
reaper + idempotency exist, even an *ungraceful* death is merely slow, not
lossy — the lease expires, the task re-runs, the atomic commit makes the re-run
harmless. Graceful shutdown is an optimization on top of a crash-safe design,
never a substitute for one. The checklist's specific demand: never abort a task
in a way that loses its ack (kill the encode *or* let it settle — don't kill the
settle).

## 7. Mental model summary

| Fundamental | The one-liner |
| --- | --- |
| `202` + pollable resource | Record durably, answer immediately, let callers watch the DAG drain |
| Argv, never shell | The kernel doesn't parse `;` — only a shell does; keep every invocation inside `ffmpeg::run` |
| Traversal guard | `join` betrays you on `..` and absolute paths; canonicalize, then require the `WORK_DIR` prefix |
| Auth on submit | An open transcode endpoint is free compute + a DoS multiplier; bound the ladder too |
| Straggler observability | Histogram (tail), gauges (depth/utilization), counters (reclaims, attempts, cache hits), a span per task |
| Graceful shutdown | Stop claiming → drain → let leases cover whatever's left; grace is an optimization over crash-safety |

## 8. Where these land

No single module — that's the point of "woven through": the auth + validation
TODO sits on [`submit`](../src/routes.rs), the traversal `todo!()` in
[`resolve_source`](../src/job.rs), argv discipline inside your V3/V4 ffmpeg
calls, metrics beside each store transition, and shutdown ordering in
[`main.rs`](../src/main.rs)'s wiring. Each checklist box flips only when its
criterion is *observably* true — same rule as the verticals.
