---
description: Format, lint, type-check and test (every project, or one project)
argument-hint: [project, e.g. "01" or "url-shortener" — omit for every project]
allowed-tools: Bash(make *), Bash(uv *), Bash(cd *)
---

Run the quality gate for: **${ARGUMENTS:-every project}**

1. If an argument names a project, resolve it to `projects/NN-name/`; otherwise run
   the gate in every `projects/*/` that has a `pyproject.toml`, then in `packages/*`.
2. From each project dir run `make verify`, which runs, in order:
   - `uv run ruff format --check .` (offer `make fmt` if it fails)
   - `uv run ruff check .`
   - `uv run pyright` (strict)
   - `uv run pytest -q`
3. Summarize pass/fail per step and per project. Remember: **verticals that still
   `raise NotImplementedError` are the expected scaffold state** — scaffold tests
   assert that they raise; call those out as benign, don't treat them as failures.
   Flag everything else.
4. Do not fix issues unless asked — just surface them clearly with `file:line`.
