---
description: Convert a scaffold-only project from Rust to Python — regenerated from its SPEC, never translated from the Rust
argument-hint: <project, e.g. "07" or "distributed-cache"> [--force]
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

Convert to Python: **$ARGUMENTS**

This is a LEARNING repo. You are porting **scaffolding only** — signatures, wiring,
`TODO(Vx)` markers. The interesting logic stays unwritten. `raise NotImplementedError`
is the Python `todo!()`: it panics at runtime by design, and it is the worklist.

> **The rule that matters most:** do **not** translate the Rust line by line. Rust
> scaffolds carry Rust shapes — `Result` enums, traits, `&mut self`, ownership hints —
> and dragging those into Python produces Rust-in-Python, which teaches the wrong
> habits for a Python day job. **`SPEC.md` is the source of truth.** Read the Rust
> module doc-comments for the *teaching* (they explain why the primitive is hard),
> then re-derive the Python surface from scratch, idiomatically.

## 0. One-time repo plumbing (idempotent — check first, skip if present)

Only on the first conversion. Verify each before doing it:

1. **`tools/makefile_runner.py`** — add a `register_python_checks(runner)` bundle
   mirroring `register_cargo_checks` (line ~293), and a `uv()` method beside
   `cargo()`. Tasks: `lint` (ruff check), `fmt` (ruff format), `fmt-check`,
   `types` (pyright), `test` (pytest), `verify` (fmt-check → lint → types → test),
   `clean`. Same emojis and groups so every project still feels the same.
2. **`tools/status.py`** — make the tracker polyglot, surgically:
   - `SRC_RE` (line ~95) → `r"src/([\w/]+\.(?:rs|py))"`
   - `TODO_RE` (line ~98) → also match `r"\braise\s+NotImplementedError\b"`
   - the display strings at lines ~731 and ~771 hardcode `todo!()` — derive the
     label from the module's suffix instead.
3. **Root `pyproject.toml`** — add `[tool.uv.workspace]` with an **explicit**
   `members` list (not a `projects/*` glob — that matches the still-Rust project
   dirs, which have no `pyproject.toml`). Append each project as you convert it.
   This is the direct analogue of the Cargo workspace: members declare deps, one
   root `uv.lock` pins every version. Keep the existing `[tool.ruff]`/`[tool.pyright]`
   config for the stdlib-only `tools/` scripts. The root has no `[project]` table —
   it is a *virtual* workspace root.
4. **`packages/common_telemetry/` + `packages/common_config/`** — Python siblings of
   `crates/common-*`. These are **fully implemented** (CLAUDE.md's sanctioned
   exception to "don't write the meat"): structlog JSON logging with a request id,
   `prometheus_client` registry + `/metrics` router, pydantic-settings base config.
5. **`.github/workflows/ci.yml`** — a `python` job (ruff + pyright strict + pytest),
   gated on the existing `changes` filter.
6. **`CLAUDE.md`** — record the Python conventions in "Layout & conventions" so this
   doesn't have to be re-derived every time.

## 1. Gate: refuse projects with real work

Run `make status NN`. **Convert only if verticals done == 0 and checklist done == 0.**
Projects with real implementations — **01, 03, 04, 06, 13** — stay Rust; rewriting
code the owner already understood costs weeks and teaches nothing. If the target has
work in it, stop and say so unless `--force` was passed.

## 2. Produce the layout

`src`-layout package, because that's what a real Python service looks like:

```
projects/NN-name/
├── SPEC.md              # kept, rewritten in place (§3)
├── CONCEPTS.md          # kept as-is
├── docs/                # kept as-is — the teaching is language-agnostic
├── pyproject.toml       # new — deps, ruff, pyright strict
├── .python-version      # 3.13+
├── docker-compose.yml   # UNCHANGED — host ports stay NN-scoped (54NN/63NN/…)
├── .env.example         # unchanged unless a var was Rust-specific
├── Makefile             # thin wrapper (§4)
├── makefile.py          # register_python_checks (§4)
├── src/<package_name>/  # crate name with underscores: distributed_cache
│   ├── __init__.py
│   ├── main.py          # wiring COMPLETE — config, lifespan, router, graceful shutdown
│   ├── config.py        # pydantic-settings, one field per .env.example var
│   ├── errors.py        # AppError → HTTP, mirrors error.rs's mapping
│   ├── routes.py
│   └── <one module per vertical>.py
└── tests/
```

Stack — chosen for day-job transfer, not novelty:

| Concern | Use |
|---|---|
| Toolchain | **uv** (never pip/poetry); `uv.lock` committed |
| HTTP | FastAPI + uvicorn, `uvloop` loop |
| Validation / config | pydantic v2 + pydantic-settings |
| DB | SQLAlchemy 2.0 async — **except** where the SPEC's lesson *is* the SQL (e.g. 04's `SKIP LOCKED`): then asyncpg with raw, parameterized SQL |
| Logging / metrics | structlog + prometheus-client (via `packages/common_*`) |
| Tests | pytest + pytest-asyncio + httpx `ASGITransport` |
| Types / lint | pyright **strict** + ruff |

Per module: keep the Rust file's doc-comment prose (translate to a module docstring —
it's the teaching), give every public function a real type annotation, and body it with
`raise NotImplementedError`. **Re-aim each `TODO(Vx)` hint at a Python data structure** —
e.g. a `BTreeMap` range-scan hint becomes `bisect` over a sorted list or
`sortedcontainers.SortedDict`. Never let a hint name a Rust type.

`main.py` is wired **completely** (the app starts, `/healthz` answers, shutdown drains);
only vertical modules raise.

> **The uvloop trap.** Production runs on **uvloop** (uvicorn[standard], `loop="auto"`),
> but pytest-asyncio runs on the **stdlib** loop. uvloop does not implement the
> `loop.sock_*` family — `loop.sock_recvfrom` raises `NotImplementedError` there while
> working fine under pytest. For UDP always use `loop.create_datagram_endpoint` with a
> `DatagramProtocol`, bridged into a **bounded** `asyncio.Queue` (the bound is usually a
> SPEC backpressure criterion). Never scaffold a raw socket + `loop.sock_recvfrom`.

## 3. Rewrite `SPEC.md` in place

Keep the two-axis structure, the Done-when/Proof format, and the status block. Change:

- Module paths: `src/foo.rs` → `src/<package_name>/foo.py`. Keep each `### Vn. <title>`
  heading and name its module once inside that vertical — the tracker maps from it.
- Toolchain mentions: `cargo test` → `pytest`, `clippy` → `ruff + pyright`.
- **Boss fight: leave every number exactly as written.** The Rust targets stay. Where
  CPython can't reach one, that gap *is* the finding — `docs/NN-benchmarks.md` records
  where it topped out and why (GIL contention? GC pauses? allocation? a blocking call
  on the loop?). Do not quietly scale targets down.
- `## 🔬 From the field` — keep verbatim, boxes stay `[~]`/`[✔]`.

**Extend the horizontal checklist** with the Python-specific items — this axis is the
day-job curriculum, so it gets promoted, not trimmed:

- [ ] **pyright strict passes clean** — every `# type: ignore` carries a justifying comment.
- [ ] **No blocking call on the event loop** — runs clean under `PYTHONASYNCIODEBUG=1`; any sync I/O is in a thread/process pool deliberately.
- [ ] **Bounded pool sized on purpose** — pool size and worker count tuned *together*, with the reasoning in the design doc.
- [ ] **Graceful shutdown** drains in-flight requests on SIGTERM via the FastAPI lifespan.
- [ ] **Profile committed** — a `py-spy` flamegraph and a `memray` run in `docs/NN-benchmarks.md`, naming the top bottleneck.

Add the profile to the **Definition of done** too: numbers alone don't close it, you
have to know *why* they are what they are.

## 4. Task runner

Regenerate `makefile.py` + `Makefile` following `/gen-makefile`, swapping
`register_cargo_checks` for `register_python_checks`. Keep every compose/db/redis
bundle and all project-specific tasks — those are unchanged by the language.

## 5. Detach from Cargo

- Delete `src/*.rs`, `Cargo.toml`, and any `benches/`, `.sqlx/`, `build.rs`.
- Remove the crate from root `Cargo.toml` `members`.
- Run `cargo hakari generate` (the dependency graph shrank).
- Point the project's `.github/dependabot.yml` block at the `uv` ecosystem.
- Git history keeps the Rust — don't preserve it in-tree.

## 6. Verify

Run from the project dir: `uv sync`, `make verify`, `make run` + `curl /healthz`.
Then `cargo check --workspace` from the root to confirm nothing else broke, and
`make status NN` to confirm the tracker still reads the verticals and now counts
`NotImplementedError` as the worklist.

**Then boot the container** — `docker build -f projects/NN-*/Dockerfile .` and run it.
This is not optional: it is the only check that exercises uvloop and PID-1 signal
handling, and it catches loop-implementation bugs that `make verify` cannot see. If
host port publishing is unavailable, probe from inside with
`docker exec <c> curl -sf http://127.0.0.1:<port>/healthz` and confirm a clean
`Application shutdown complete` on `docker stop`.

## 7. Report

Say what was created, what was deleted, which plumbing steps ran vs. were already
present, and the suggested first vertical to attack. **Do not start implementing.**
