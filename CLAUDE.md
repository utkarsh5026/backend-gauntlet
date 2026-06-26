# CLAUDE.md — backend-gauntlet

A progression of backend/infrastructure projects in Rust, easy → hard, built to
learn Rust + scale + backend fundamentals. See `README.md` for the full roadmap.

## ⚠️ This is a LEARNING repo — default behavior

**The owner writes the interesting code themselves.** Projects are scaffolded with
`todo!()` markers and a `SPEC.md` "ticket". Do **NOT** implement the `todo!()` bodies
or solve the SPEC challenges unless the user *explicitly* asks ("implement V1",
"write the cache for me"). By default, prefer in this order:

1. **Explain / teach** the concept and tradeoffs.
2. **Hint** — graduated nudges, not full solutions (see `/hint`).
3. **Review** what the user wrote against the SPEC (see `/spec-review`).
4. **Scaffold** new structure (signatures, TODOs, SPECs) — never the meat.

Only write complete solution code when asked outright. When you do, explain it.

## The "two-axis" SPEC convention

Every project's `SPEC.md` grades on two axes — keep this when scaffolding new ones:
- **Vertical (V1, V2, …):** the scale primitive's internals, built from scratch
  (the parts you'd normally `cargo add`). Each has a "concept to internalize".
- **Horizontal checklist:** protocols, caching, security, observability — woven in.
- A **Definition of done** that requires a `bench/` with real numbers + a design doc.
- SPECs describe *what* and *why*, never the solution. No spoilers.

## Layout & conventions

- **Cargo workspace.** All dependency versions live in the root `[workspace.dependencies]`.
  Member crates use `foo = { workspace = true }` — never pin versions in a member.
- `crates/common-*` are **fully implemented** shared helpers (telemetry, config),
  reused by every project. These are the exception to "don't write the meat".
- `projects/NN-name/` — one project each. Binary crate name omits the number
  (e.g. `projects/01-url-shortener` → package `url-shortener`).
- Per-project module convention: `main.rs` (wiring), `error.rs` (AppError→HTTP),
  `routes.rs`, plus one module per vertical challenge.
- Telemetry: use `common-telemetry::init(...)` and `common-config` for env/secrets.
  OTel/Prometheus are added per-project, not in the base crate (keeps builds stable).

## Commands

```bash
cargo check --workspace            # fast type-check everything
cargo clippy --workspace -- -D warnings
cargo fmt --all
cargo run -p <crate>               # e.g. -p url-shortener
cargo test -p <crate>

# per project (run from projects/NN-*/):
docker compose up -d               # start its deps (postgres/redis/…)
sqlx migrate run                   # apply migrations (needs sqlx-cli + DATABASE_URL)
```

`todo!()` bodies **panic at runtime** by design — that's the worklist, not a bug.
A clean `cargo check` with only dead-code warnings is the expected scaffold state.

## Style

- Match surrounding code; idiomatic async Rust (tokio/axum).
- Errors via `thiserror`/`anyhow`; handlers return `Result<T, AppError>` and use `?`.
- `sqlx` compile-time-checked queries (`query!`) once a DB is available — no string SQL.
- Never log secrets/API keys. Never commit `.env` (it's gitignored).
