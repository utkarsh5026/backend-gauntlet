---
description: Format, lint, type-check and test (whole workspace, or one project)
argument-hint: [project, e.g. "01" or "url-shortener" — omit for whole workspace]
allowed-tools: Bash(cargo *), Bash(cd *)
---
   
Run the quality gate for: **${ARGUMENTS:-the whole workspace}**

1. If an argument names a project, resolve it to the crate (e.g. `01` →
   `url-shortener`) and scope commands with `-p <crate>`; otherwise use `--workspace`.
2. Run, and report results concisely:
   - `cargo fmt --all -- --check` (offer to run `cargo fmt --all` if it fails)
   - `cargo clippy <scope> -- -D warnings`
   - `cargo check <scope>`
   - `cargo test <scope>`
3. Summarize pass/fail per step. Remember: **dead-code warnings from unimplemented
   `todo!()` scaffolding are expected** — call those out as benign, don't treat them
   as failures. Flag everything else.
4. Do not fix issues unless asked — just surface them clearly with `file:line`.
