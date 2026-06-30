---
description: Generate a Python-backed makefile.py task runner + thin Makefile wrapper for a project
argument-hint: <project, e.g. "02" or "rate-limiter" — omit for the current directory>
allowed-tools: Bash(make *), Bash(python3 *), Bash(ls *), Bash(cat *), Read, Write, Edit
---

Generate a task runner for: **${ARGUMENTS:-the current project}**

The canonical reference implementation lives at
`projects/01-url-shortener/makefile.py` and `projects/01-url-shortener/Makefile`.
Treat those two files as the **template** — copy their structure and adapt the
*tasks* to the target project. Do NOT redesign the runner; keep it consistent
across projects so every project feels the same.

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

Copy the template and keep ALL of its infrastructure verbatim — only the task
set changes:

- The module docstring (retitle to the target crate).
- `class C` colors + `step`/`ok`/`warn`/`fail` helpers.
- `_rule` + `banner_start` / `banner_end` (start/finish banners with timing).
- `run()` (echoes the dimmed `$ cmd`, exits on failure), `cargo()`,
  `load_dotenv()`, `require()`.
- The `@task(name, emoji, group, help)` registry + auto-generated grouped `help`.
- `run_task()` wrapping each top-level task in banners + try/except for
  `SystemExit`/`Exception`, and `main()` propagating non-zero exit codes.
- The `sys.stdout.reconfigure(line_buffering=True)` guard in `__main__` (keeps
  banner/stderr ordering correct when piped).

Adapt the **constants** (`CRATE`, `COMPOSE`, paths) and the **task functions** to
the services/vars you found in step 1. Reuse the same emojis and groups
(`Setup` / `Services` / `Checks` / `Run` / `Bench` / `Meta`) so projects stay
uniform. `wait-db` (or equivalent) must poll the real healthcheck for the actual
service+user from `docker-compose.yml`. Composite tasks (`verify`, `dev`,
`reset-db`) call the other task *functions* directly so only the outer banner shows.

## 3. Generate the thin `Makefile`

Copy the template Makefile: it just lists the task names in `TASKS` and forwards
each to `python3 makefile.py $@`. Keep the `TASKS` list in sync with the
`@task` registry you generated. `.DEFAULT_GOAL := help`.

## 4. Verify

Run `make help` (and `python3 makefile.py help`) from the project dir and confirm
the grouped, emoji'd help renders. Spot-check one safe task (e.g. `make fmt-check`
or `make smoke`) to confirm the start/finish banners and exit-code propagation
work. Report what tasks were included/skipped and why.
