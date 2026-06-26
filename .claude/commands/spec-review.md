---
description: Review your implementation of a project against its SPEC like a senior reviewer
argument-hint: <project, e.g. "01" or "01-url-shortener", optionally a specific challenge like "V1">
---

Review the user's implementation for project: **$ARGUMENTS**

Act as a senior backend engineer reviewing a teammate who is learning. This is a
LEARNING repo — your job is to make them better, not to rewrite their code.

1. Read the project's `SPEC.md` (vertical challenges + horizontal checklist +
   definition of done) and their current `src/`.
2. Verify it builds and check what's actually implemented vs still `todo!()`:
   `cargo clippy -p <crate> -- -D warnings` and `cargo test -p <crate>`.
3. Review against the SPEC, organized by:
   - **Correctness** — bugs, race conditions, edge cases (clock skew, overflow,
     empty input, concurrent access).
   - **Scale** — does it hold up under load? hot paths, N+1 queries, blocking the
     async runtime, unbounded growth, lock contention.
   - **Security** — the SPEC's security checklist (auth, validation/SSRF, injection,
     secret handling, constant-time compares).
   - **Idiomatic Rust** — ownership, error handling, unnecessary clones/allocations.
   - **Checklist coverage** — which SPEC boxes are genuinely done vs missing.
4. For each finding: severity, the `file:line`, *why* it matters, and a hint toward
   the fix — but **do not apply fixes** unless the user explicitly asks.
5. End with a short prioritized list of what to tackle next.

Be direct and specific. Praise what's genuinely good; don't pad.
