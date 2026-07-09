---
description: Write a first-principles teaching doc for a topic into a project's docs/ folder, grounded in that project's real code
argument-hint: <topic> [in project NN], e.g. "how paths work in S3 in project 06" or "content-addressed storage"
---

Write a deep, first-principles teaching document about: **$ARGUMENTS**

This is a LEARNING repo (see CLAUDE.md). A teaching doc is the sanctioned
"explain/teach" mode — but it must **teach the concept and how the *existing* code
works**, never spell out the solution to an unsolved `todo!()` / SPEC challenge. If
the topic can't be explained without handing over unwritten solution code, stop and
offer a `/hint` instead.

## 1. Resolve the target project and topic

- Parse the topic and, if named, the project (`in project NN`, a project name, or a
  file the topic clearly belongs to). If not named, infer from the current IDE
  selection / files in play; if still ambiguous between projects, ask which one.
- The doc lives in **that project's** `docs/` dir: `projects/NN-name/docs/`. Create
  `docs/` if it doesn't exist.

## 2. Ground everything in this project's real code

- **Read the actual source** in that project that the topic touches before writing a
  word — the relevant `src/*.rs`, `SPEC.md`, routes, tests. Every structural claim in
  the doc must be anchored to real code that exists, cited with a **relative markdown
  link** (e.g. `[encode_key](../src/index.rs)`), not invented or generic.
- Prefer this project's concrete types, function names, and on-disk layout over
  textbook abstractions. The reader should be able to jump from any claim to the line
  that backs it.

## 3. Verify every factual / computed claim

- Anything you can check, **check with a real tool before asserting it** — hashes
  (`sha256sum`), encodings, command output, byte values, test results (`cargo test`).
  Do not hand-write a digest or a "this test passes" claim from memory. If a test
  actually fails or the code diverges from the ideal, say so honestly in the doc.

## 4. Write it — first principles, no assumed knowledge

Follow the house teaching style (concrete-scenario-first, derive-don't-declare, no
assumed background). Aim for a doc a beginner can follow end to end:

- A short blockquote intro: what this teaches, "no prior knowledge assumed", and the
  code files it's anchored to.
- **"The one sentence to hold onto"** — the single core idea, up top.
- **The problem before the solution** — why the naive approach fails (a table of
  concrete failure modes works well), motivating the design that follows.
- **Concrete worked examples** — real keys/bytes/paths/values traced through, in
  tables and ASCII diagrams, not prose alone.
- **An end-to-end trace** — follow one real request/operation through every layer.
- **A mental-model summary table** (looks-like vs. actually-is) and a **"where to
  look in the code"** index mapping subtopics → files.

Use the existing project docs as the quality bar (e.g.
`projects/01-url-shortener/docs/`, `projects/06-object-store/docs/`).

## 5. Filename & finish

- Write to `projects/NN-name/docs/<PP>-<kebab-topic>.md`, where `<PP>` is the next
  zero-padded 2-digit prefix after the highest already in that `docs/` dir (start at
  `00`).
- Report the path created and give a 2–3 line summary of what it covers. Don't start
  implementing anything in `src/`.
