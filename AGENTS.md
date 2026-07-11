## Learned User Preferences

- Prefers fewer merged helpers over many small single-purpose functions when the logic is trivial (e.g., one-liner wrappers around `HashSet::contains`).
- Keeps test-only fixtures/builders inside `#[cfg(test)] mod tests`; helpers used by production code stay in the main module even when primarily tested there.
- When inlining or merging helpers, preserves test coverage (e.g. router/middleware integration tests if unit-test-only helpers are removed).
- Wants `unsafe` blocks documented with both safety rationale and performance/allocation motivation (e.g., avoiding a second `String` allocation).

## Learned Workspace Facts

- `rustfmt` and format-on-save do not reformat bodies inside proc macros such as `proptest!`.
- Active first project is `projects/01-url-shortener`; local dev uses its Makefile (`make verify`, `make dev`) and Docker Compose for Postgres/Redis.
- Workspace `.vscode` settings point the integrated terminal cwd and env file at `projects/01-url-shortener`.
