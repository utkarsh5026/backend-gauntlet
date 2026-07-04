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

**The one sanctioned exception:** during a `/quest`, Claude writes **black-box
acceptance tests** (`tests/<module>_acceptance.rs`) derived from a vertical's
"Done when ALL true" criteria — *before* the user implements, so they can't encode
the solution. Tests drive the public surface only; implementation stays the user's.

## The "two-axis" SPEC convention

Every project's `SPEC.md` grades on two axes — keep this when scaffolding new ones:
- **Vertical (V1, V2, …):** the scale primitive's internals, built from scratch
  (the parts you'd normally `cargo add`). Each has a "concept to internalize".
- **Horizontal checklist:** protocols, caching, security, observability — woven in.
- A **Definition of done** that requires a `bench/` with real numbers + a design doc.
- The bench requirement is staged as a **🐉 Boss fight**: a named, themed load/failure
  scenario (e.g. "The Thundering Herd") right after the Definition of done, with a
  short flavor paragraph, an **Arena** line (how the fight runs), a **"The boss falls
  when ALL true"** `- [ ]` block of explicit numeric targets (RPS, p99, hit ratio —
  observable outcomes, still no solution steps), and a **Proof** line pointing at the
  benchmark doc. One boss per project (reference: `projects/01-url-shortener/SPEC.md`).
  `status.py` counts the boss's boxes in the horizontal checklist — that's intended.
- SPECs describe *what* and *why*, never the solution. No spoilers.
- **Acceptance-criteria format** (reference: `projects/01-url-shortener/SPEC.md`):
  every challenge — verticals *and* the Definition of done — carries a **"Done when
  ALL true"** block of `- [ ]` criteria that are **observable outcomes, never
  solution steps**, plus a **"Proof"** line naming the test/bench/doc that
  demonstrates it. A box only flips to ✅ when its Proof exists. The SPEC opens with
  a one-paragraph "How to read this SPEC" note stating this. `status.py` counts the
  per-vertical "Done when" boxes toward that vertical (its `[done/total]` bar) and
  the horizontal checklist separately, so don't double-list a vertical's criteria in
  the horizontal section.
- Each `SPEC.md` opens with a render-invisible status block (used by the tracker):
  ```html
  <!-- status:
  state: active            # active | paused | blocked | done | not-started
  blocked-on: ~            # free text, or ~ for none
  -->
  ```
  Keep each vertical heading as `### Vn. <title>` and name its `src/<module>.rs`
  once inside that vertical's section — the tracker maps vertical → module from it.

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
- **Docker host ports are project-scoped:** host port = the service's conventional
  port with the last two digits replaced by the project number `NN` (postgres →
  `54NN`, redis → `63NN`, nats → `42NN`, clickhouse-http → `81NN`, …). Only the
  *host* side of the mapping changes (`"5404:5432"`); container-internal ports stay
  canonical, so healthchecks and service-to-service URLs are untouched. Apply this
  when scaffolding any new project, and keep `.env.example` + code fallback URLs in
  lockstep with the compose file.

## Commands

```bash
make status                        # progress dashboard across all projects
make status NN=02                  # drill into one project (verticals + open items)
make trophies                      # 🏆 achievements (auto-derived — never award by hand)
make infra                         # web panel: per-project Docker deps, up/down, port conflicts

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
