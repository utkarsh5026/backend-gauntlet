---
description: Stage (if needed) and create a meaningful Conventional Commit
argument-hint: [optional hint/scope, e.g. "project 01 cache" or "wip"]
allowed-tools: Bash(git *)
---

Create a high-quality commit for the current changes. Optional hint: **$ARGUMENTS**

1. Inspect state in parallel: `git status`, `git diff --stat`, `git diff` (staged
   and unstaged), and `git log --oneline -10` to match the repo's message style.
2. If nothing is staged, stage the relevant changes (`git add -A` unless the hint
   says otherwise). Never stage `.env` or other secrets — verify they're ignored.
3. Group the work into ONE logical commit (or tell the user if it should be split).
4. Write a **Conventional Commit** message:
   - Header: `type(scope): summary` — imperative, ≤72 chars. Types: `feat`, `fix`,
     `chore`, `docs`, `refactor`, `test`, `perf`, `ci`, `build`. Scope = the project
     or area (e.g. `01-url-shortener`, `ci`, `workspace`, `common-config`).
   - Body (when non-trivial): *why* the change exists and any notable decisions —
     not a restatement of the diff. Wrap at ~72 cols.
   - Note unfinished `todo!()` scaffolding as intentional when relevant.
5. End the message with this trailer (own line, blank line before it):
   `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
6. Show the proposed message, then commit with `git commit` (use a HEREDOC for the
   multi-line message). Report the resulting short hash. Do NOT push.
