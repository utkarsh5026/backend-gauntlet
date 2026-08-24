---
description: Scaffold the next project in the roadmap following the two-axis SPEC convention
argument-hint: <which project, e.g. "23" or "dynamodb"> [--rust]
---

Scaffold a new project: **$ARGUMENTS**

Follow the conventions in CLAUDE.md exactly. This is a LEARNING repo — scaffold
structure and a SPEC, but leave the interesting logic unimplemented. Do not solve it.

**Python is the default.** The roadmap is moving off Rust (see `/pythonize`); new
projects are Python unless `--rust` is passed. In Python the worklist marker is
`raise NotImplementedError` — the exact analogue of `todo!()`, and `tools/status.py`
counts it the same way.

1. Identify the project from `README.md`'s roadmap. Confirm the number/name and its
   place in the tier if ambiguous. If it is not on the roadmap yet, add its row.
2. Create `projects/NN-name/` with:
   - **`SPEC.md`** in the two-axis, **acceptance-criteria** format (see
     `projects/01-url-shortener/SPEC.md` as the reference): a short framing of why
     this primitive is hard at scale; a one-paragraph **"How to read this SPEC"**
     note explaining the Done-when/Proof convention; **Vertical challenges** (V1,
     V2, … — the internals to build from scratch). Each vertical keeps its prose +
     a **"concept to internalize"** and adds a **"Done when ALL true"** block of
     `- [ ]` criteria that are *observable outcomes, never solution steps* (no
     spoilers), plus a **"Proof"** line naming the test/bench/doc that demonstrates
     it. Then a **Horizontal checklist** (protocols / caching / security /
     observability relevant to THIS project); cross-cutting scale skills; a
     **Definition of done** framed as "done when ALL true" — every box checked with
     its Proof, the boss defeated, a design doc, and a green check gate; and a
     **🐉 Boss fight** right after it — the project's bench requirement staged as a
     named, themed load/failure scenario (flavor paragraph, an **Arena** line, a
     "The boss falls when ALL true" `- [ ]` block of explicit numeric targets like
     RPS / p99 / hit ratio — observable outcomes, no solution steps — and a Proof
     line pointing at `docs/NN-benchmarks.md`); and a "suggested order of attack".
     Name the boss after the failure mode the primitive exists to defeat (e.g.
     stampede → "The Thundering Herd", backpressure → "The Flood").
     **Do not scale boss numbers down because it's Python** — where CPython can't
     reach a target, the gap and its cause is the finding.
   - **`pyproject.toml`** — deps, `[project.scripts]` console entry named like the
     project, `[tool.pyright] typeCheckingMode = "strict"`, ruff, pytest
     (`asyncio_mode = "auto"`). Workspace deps via `[tool.uv.sources] … { workspace = true }`.
   - `docker-compose.yml` for its dependencies (with healthchecks), `.env.example`,
     `.python-version` (3.13+), and `migrations/` if it uses a DB.
     **Host ports are project-scoped** (postgres `54NN`, redis `63NN`, …) — only
     the host side; container-internal ports stay canonical.
   - `src/<package_name>/` (crate name with underscores), **src layout**:
     `main.py` (wiring COMPLETE — config, lifespan, router, graceful shutdown,
     `common_telemetry` middleware + metrics routes), `config.py` (pydantic-settings,
     one field per `.env.example` var), `errors.py` (AppError→HTTP), `routes.py`,
     `state.py`, and one module per vertical with `TODO(Vx)` comments and
     `raise NotImplementedError` bodies. Reuse `packages/common-{config,telemetry}`.
   - `tests/` — a `conftest.py` harness (app fixture over `httpx.AsyncClient` +
     `ASGITransport`, **not** Starlette's `TestClient`, which is typed against
     `httpx2`) and a smoke test proving the app boots. Never write the acceptance
     tests here — those are `/quest`'s job.
   - `Makefile` + `makefile.py` via `/gen-makefile`, using `register_python_checks`
     and `register_python_run`.
   - `Dockerfile` if it has a compose file: uv-based, multi-stage, **both stages on
     the same base image** (a venv is not relocatable across Python installs).

   Stack: **FastAPI + uvicorn, pydantic v2, httpx, structlog, prometheus-client**.

   > **The uvloop trap.** Production runs uvloop (`uvicorn[standard]`), pytest runs
   > the stdlib loop. uvloop does not implement the `loop.sock_*` family, so code
   > using them passes tests and dies in Docker. For UDP use
   > `loop.create_datagram_endpoint` bridged into a **bounded** `asyncio.Queue`.

3. Add the project to `[tool.uv.workspace] members` in the root `pyproject.toml`
   (an explicit entry — the list is not a glob).
4. If the project has its own `docker-compose.yml`, add a `docker`
   `package-ecosystem` block to `.github/dependabot.yml` pointing at
   `/projects/NN-name` (each compose dir needs its own; the `uv` and `actions`
   blocks already cover the whole repo).
5. Include a **Python & runtime** section in the horizontal checklist — this axis is
   the day-job curriculum, so it is promoted, not trimmed: pyright strict clean; no
   blocking call on the event loop (`PYTHONASYNCIODEBUG=1`); bounded pools sized on
   purpose; graceful shutdown draining in-flight work; and a profiling gate
   (`py-spy` flamegraph + `memray`) in the Definition of done.
6. Run `uv sync` and `make verify` from the project dir and confirm it is green
   (ruff, pyright strict, pytest). A clean run with every vertical still raising is
   the expected scaffold state.
7. Summarize what was created and the suggested first move — do not start implementing.

## If `--rust` was passed

Scaffold Rust instead: `Cargo.toml` (deps via `{ workspace = true }`, new shared
deps added to the root `[workspace.dependencies]` first), `src/` with `main.rs`
(wiring complete), `error.rs`, and one module per vertical with `todo!()` bodies
wired to `common-telemetry` / `common-config`. Add the crate to the workspace
`members` list, run `cargo hakari manage-deps`, and verify with
`cargo check --workspace` (only dead-code warnings are acceptable).
