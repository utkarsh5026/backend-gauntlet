## Learned User Preferences

- Prefers fewer merged helpers over many small single-purpose functions when the logic is trivial (e.g., one-liner wrappers around a set membership check).
- Keeps test-only fixtures/builders in the test modules; helpers used by production code stay in the main module even when primarily tested there.
- When inlining or merging helpers, preserves test coverage (e.g. router/middleware integration tests if unit-test-only helpers are removed).

## Learned Workspace Facts

- The repo is Python-only (uv workspace); there is no Cargo workspace or Rust toolchain.
- Active project is `projects/13-live-ingest` (RTMP → LL-HLS), now Python; no Docker/DB — run with `make run` (`uv run live-ingest`), optional `web/` player via Bun.
- Workspace `.vscode` settings point the integrated terminal cwd and env file at `projects/13-live-ingest`.
