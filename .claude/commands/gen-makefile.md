---
description: Generate a Python-backed makefile.py task runner + thin Makefile wrapper for a project
argument-hint: <project, e.g. "02" or "rate-limiter" — omit for the current directory>
allowed-tools: Bash(make *), Bash(python3 *), Bash(ls *), Bash(cat *), Read, Write, Edit
---

Generate a task runner for: **${ARGUMENTS:-the current project}**

The canonical reference implementations are `projects/14-media-transport/makefile.py`
(no compose, probe-heavy) and `projects/13-live-ingest/makefile.py` (with a `web/`
frontend). Shared infrastructure lives in `tools/makefile_runner.py`; help tables in
`tools/makefile_help.py`. Treat those as the **template** — register common bundles
and add project-specific `@runner.task` handlers. Do NOT redesign the runner; keep it
consistent across projects so every project feels the same.

## 1. Resolve & inspect the target project

1. Resolve the argument to a project dir under `projects/NN-name/` (e.g. `02` →
   `projects/02-rate-limiter`). If omitted, use the current working directory.
   Derive `CRATE` = the `[project] name` in that project's `pyproject.toml` (it is
   also the console script `make run` invokes).
2. Read the project to learn which tasks actually apply — **do not assume**:
   - `docker-compose.yml` → which services exist (postgres? redis? something
     else?), their user/port, and healthcheck commands. This drives `up`/`down`/
     `ps`/`logs`/`wait-*`/`reset-*`.
   - `.env.example` → the env vars (`PORT`, `HTTP_PORT`, `DATABASE_URL`, etc.) and
     whether a `setup` (copy `.env.example` → `.env`) task makes sense.
   - `migrations/` → a `migrate` task that runs the project's own migration entry
     point (the app applies its schema; there is no external migration CLI).
   - `bench/` → the `bench-*` tasks; skip if it doesn't exist.
   - `web/` / `dashboard/` → `register_dev_stack` for `dev` / `frontend` / `web-install`.

## 2. Generate `makefile.py`

Use `tools/makefile_runner.py` — do **not** copy-paste infrastructure into each
project. Pattern:

```python
from makefile_runner import (
    make_runner,
    register_setup,
    register_python_checks,
    register_python_run,
    register_compose_lifecycle,
    register_redis,      # if Redis
    register_smoke_healthz,
    register_dev_stack,  # if web/ or dashboard/
    register_md,
    register_help,
)

runner = make_runner(crate=CRATE, help_title="…", project_dir=PROJECT_DIR, …)
register_setup(runner)
register_python_checks(runner)
register_python_run(runner)
# … register bundles that apply …

@runner.task("up", "🐳", "Services", "…")
def up(): …

register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
```

**Shared bundles** (from `makefile_runner.py`):

| Helper | Tasks |
|--------|-------|
| `register_setup` | `setup` |
| `register_python_checks` | `check`, `lint`, `fmt`, `fmt-check`, `types`, `test`, `verify`, `clean` |
| `register_python_run` | `run` (`uv run <crate>` with `.env` loaded) |
| `register_compose_lifecycle` | `down`, `ps`, `logs` |
| `register_redis(runner, default_port=…)` | `wait-redis`, `ensure_redis` helper; optional `reset` |
| `register_smoke_healthz` | `smoke` (curl `/healthz` on `PORT`) |
| `register_dev_stack(runner, vite_port=…)` | `dev` (+ `web-install` / `frontend` when a UI exists) |
| `register_md` | `md` (glow) |
| `register_help` | `help` (Rich tables via `makefile_help.py`) |

Adapt **constants** (`CRATE`, `default_port`, bundle params) and add **project-specific**
`@runner.task` handlers for `up`/`deps`, probes, bench tasks, gRPC smoke, `profile`
(py-spy), etc. Reuse the same emojis and groups (`Setup` / `Services` / `Checks` /
`Run` / `Probe` / `Bench` / `Meta`). Composite tasks call other task *functions*
directly (via the dict a bundle returns) so only the outer banner shows.

## 3. Generate the thin `Makefile`

Copy the template Makefile: it just lists the task names in `TASKS` and forwards
each to `python3 makefile.py $@`. Keep the `TASKS` list in sync with the
`@runner.task` registry you generated. `.DEFAULT_GOAL := help`.

## 4. Verify

Run `make help` (and `python3 makefile.py help`) from the project dir and confirm
the grouped, emoji'd help renders. Spot-check one safe task (e.g. `make fmt-check`
or `make smoke`) to confirm the start/finish banners and exit-code propagation
work. Report what tasks were included/skipped and why.
