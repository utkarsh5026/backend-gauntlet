---
description: Add idiomatic rustdoc comments to a Rust file without changing its logic
argument-hint: <path to a .rs file, e.g. "projects/01-url-shortener/src/id_gen.rs">
---

Add rustdoc documentation comments to: **$ARGUMENTS**

You are documenting existing Rust code. **Do not change behavior** — only add doc
comments (and re-flow nothing else). This is a LEARNING repo (see CLAUDE.md), so:
do **not** fill in `todo!()` bodies or implement unfinished SPEC work. Document the
*intent* of a `todo!()` as it stands (what it's meant to do), never the solution.

1. Read the target file. If `$ARGUMENTS` is empty, ask which file. Skim sibling
   modules/`main.rs` only as needed to understand how the items are used.
2. Add documentation, outer `///` for items and `//!` for the module/crate where one
   is missing at the top of the file:
   - **Every public item** (`pub fn`, `pub struct`, `pub enum`, `pub trait`, `pub
     mod`, public fields, trait methods). Crate-private items get docs only when their
     purpose isn't obvious from the name.
   - First line: a concise one-sentence summary (imperative mood, ends with a period).
   - Then, when they apply: a short paragraph on *why*/how it fits, `# Errors` (for
     `Result`-returning fns), `# Panics` (incl. `todo!()`/`unwrap`/`expect`), `#
     Safety` (for `unsafe`), and `# Examples` with a runnable ```rust block for
     non-trivial public APIs.
   - Link items with intra-doc links (`[`Foo`]`, `[`Self::bar`]`) where helpful.
3. Match the surrounding doc style and density if the file already has some docs.
   Keep comments tight — explain what isn't obvious from the signature, don't restate
   types. No emoji, no marketing tone.
4. Leave all code, imports, and formatting otherwise untouched. Preserve existing
   `// ` line comments.
5. After editing, verify nothing broke:
   `cargo fmt -p <crate> && cargo doc --no-deps -p <crate>` (or
   `cargo check -p <crate>` if doctests aren't worth running). Report any doc warnings.

Finish with a one-line summary of what you documented and any item you intentionally
left undocumented (and why).
