---
description: Generate a Python-backed makefile.py task runner + thin Makefile wrapper for a project
argument-hint: <project, e.g. "02" or "rate-limiter" — omit for the current directory>
allowed-tools: Bash(make *), Bash(python3 *), Bash(ls *), Bash(cat *), Read, Write, Edit
---

Generate a task runner for: **${ARGUMENTS:-the current project}**

The canonical reference implementation lives at
`projects/02-rate-limiter/makefile.py` (slim, shared-runner style) and
`projects/01-url-shortener/makefile.py` (fuller example with bench tasks).
Shared infrastructure lives in `tools/makefile_runner.py`; help tables in
`tools/makefile_help.py`. Treat those as the **template** — register common
bundles and add project-specific `@runner.task` handlers. Do NOT redesign the
runner; keep it consistent across projects so every project feels the same.

## 1. Resolve & inspect the target project

1. Resolve the argument to a project dir under `projects/NN-name/` (e.g. `02` →
   `projects/02-rate-limiter`). If omitted, use the current working directory.
   Derive `CRATE` = the package name in that project's `Cargo.toml`.
2. Read the project to learn which tasks actually apply — **do not assume**:
   - `docker-compose.yml` → which services exist (postgres? redis? something
     else?), their user/port, and healthcheck commands. This drives `up`/`down`/
     `ps`/`logs`/`wait-*`/`reset-db`.
   - `.env.example` → the env vars (`PORT`, `DATABASE_URL`, etc.) and whether a
     `setup` (copy `.env.example` → `.env`) task makes sense.
   - `migrations/` + `sqlx` usage → include `migrate` / `install-tools` only if
     the project uses a DB with sqlx.
   - `benches/` (Criterion) → a `bench` task; `bench/` (Node/k6 harness) → the
     `bench-*` tasks. Skip whichever doesn't exist.
   - The crate type (bin vs lib) → include `run`/`dev`/`smoke` only for binaries.

## 2. Generate `makefile.py`

Use `tools/makefile_runner.py` — do **not** copy-paste infrastructure into each
project. Pattern:

```python
from makefile_runner import (
    make_runner,
    register_setup,
    register_cargo_checks,
    register_compose_lifecycle,
    register_postgres,   # if sqlx + Postgres
    register_redis,      # if Redis
    register_run,
    register_smoke_healthz,
    register_help,
)

runner = make_runner(crate=CRATE, help_title="…", project_dir=PROJECT_DIR, …)
register_setup(runner)
register_cargo_checks(runner)
# … register bundles that apply …

@runner.task("up", "🐳", "Services", "…")
def up(): …

register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
```

**Shared bundles** (from `makefile_runner.py`):

| Helper | Tasks |
|--------|-------|
| `register_setup` | `setup` |
| `register_cargo_checks` | `check`, `clippy`, `fmt`, `fmt-check`, `test`, `verify`, `clean` |
| `register_compose_lifecycle` | `down`, `ps`, `logs` |
| `register_postgres(runner, user=…)` | `install-tools`, `wait-db`, `migrate`, `prepare`, `reset-db` |
| `register_redis(runner, default_port=…)` | `wait-redis`, `ensure_redis` helper; optional `reset` |
| `register_run` | `run` |
| `register_smoke_healthz` | `smoke` (curl `/healthz`) |
| `register_help` | `help` (Rich tables via `makefile_help.py`) |

Adapt **constants** (`CRATE`, `default_port`, bundle params) and add **project-specific**
`@runner.task` handlers for `up`/`deps`/`dev`, bench tasks, gRPC smoke, web console,
etc. Reuse the same emojis and groups (`Setup` / `Services` / `Checks` / `Run` /
`Bench` / `Meta`). Composite tasks (`verify`, `dev`, `reset-db`) call other task
*functions* directly (via returned dict from bundles, e.g. `pg["migrate"]()`) so
only the outer banner shows.

## 3. Generate the thin `Makefile`

Copy the template Makefile: it just lists the task names in `TASKS` and forwards
each to `python3 makefile.py $@`. Keep the `TASKS` list in sync with the
`@runner.task` registry you generated. `.DEFAULT_GOAL := help`.

## 4. Verify

Run `make help` (and `python3 makefile.py help`) from the project dir and confirm
the grouped, emoji'd help renders. Spot-check one safe task (e.g. `make fmt-check`
or `make smoke`) to confirm the start/finish banners and exit-code propagation
work. Report what tasks were included/skipped and why.
