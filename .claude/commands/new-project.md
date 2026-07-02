---
description: Scaffold the next project in the roadmap following the two-axis SPEC convention
argument-hint: <which project, e.g. "02" or "rate limiter">
---

Scaffold a new project: **$ARGUMENTS**

Follow the conventions in CLAUDE.md exactly. This is a LEARNING repo — scaffold
structure and a SPEC, but leave the interesting logic as `todo!()`. Do not solve it.

1. Identify the project from `README.md`'s roadmap. Confirm the number/name and its
   place in the tier if ambiguous.
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
     its Proof, a `bench/` with numbers, a design doc, and a clippy+test green gate;
     and a "suggested order of attack".
   - `Cargo.toml` — package name without the number; deps via `{ workspace = true }`.
     Add any *new* shared deps to the root `[workspace.dependencies]` first.
   - `docker-compose.yml` for its dependencies (with healthchecks), `.env.example`,
     and `migrations/` if it uses a DB.
   - `src/` skeleton: `main.rs` (wiring complete — config, connections, router/server,
     graceful shutdown), `error.rs`, and one module per vertical challenge with clear
     `TODO(Vx)` comments and `todo!()` bodies. Wire it to `common-telemetry` /
     `common-config`.
3. Add the crate to the workspace `members` list.
4. If the project has its own `docker-compose.yml`, add a `docker`
   `package-ecosystem` block to `.github/dependabot.yml` pointing at
   `/projects/NN-name` (each compose dir needs its own block; cargo + actions
   already cover the whole repo).
5. Run `cargo check --workspace` and confirm it compiles (only dead-code warnings
   from the scaffolding are acceptable).
6. Summarize what was created and the suggested first move — do not start implementing.
