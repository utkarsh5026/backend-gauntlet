---
description: Generate a first-principles teaching doc for every core concept a project uses, derived from its SPEC — the batch, spec-driven sibling of /deep-doc
argument-hint: <project NN or name> [only Vn | only "<concept>"], e.g. "06", "02 rate limiter", or "01 only V2"
---

Generate the set of concept teaching docs for a project: **$ARGUMENTS**

One doc per core concept the project uses, so the reader can walk into the scaffold
already understanding the *ideas* — then build the `todo!()`s themselves. Think of
this as running the `/deep-doc` teaching approach across every concept in one
project's SPEC at once.

This is a LEARNING repo (see CLAUDE.md). These docs are the sanctioned "explain/teach"
mode, but they **teach the concept, never the solution**. A concept doc prepares you
to write the vertical; it must not hand over the `todo!()` body or the SPEC challenge's
answer. If a concept can't be explained without writing solution code, teach the
general idea and stop at the door — point to `/hint` and `/quest` for the build.

> **How this differs from `/deep-doc`.** `/deep-doc` writes *one* doc and walks the
> *finished* code line by line (post-implementation). `/onboard` writes *many*
> and teaches the *ideas up front* (pre-implementation), grounded in the SPEC and
> scaffolding rather than a completed solution. Both live in the same `docs/` folder
> and share the house teaching style.

## 1. Resolve the target project

- Parse the project from the argument (`NN`, a project name, or "in project NN"). If
  not named, infer from the current IDE selection / files in play; if still ambiguous,
  ask which project.
- Parse any scope narrowing: `only Vn`, `only "<concept>"`, or a comma list. Default
  is **every concept in the project**.
- Docs live in **that project's** `docs/` dir: `projects/NN-name/docs/`. Create it if
  absent.

## 2. Enumerate the concepts (the doc set) — from the SPEC, not from memory

Read the project's `SPEC.md` in full, plus `CONCEPTS.md` if it exists at the project
root. Build the concept list from these sources, in priority order:

1. **If `CONCEPTS.md` exists** — use its cards as the authoritative list. Each 🧠 card
   (mapped to its vertical + `src/<module>.rs`) becomes **one** concept doc; the card's
   "you own it when you can explain" bullets are the doc's target outcomes.
2. **Otherwise, derive from the SPEC:**
   - Each `### Vn. <title>` vertical → one concept doc. Its `*Concept to internalize:*`
     line is the doc's thesis; its **Done when ALL true** criteria are the outcomes the
     doc must make achievable; its named `src/<module>.rs` is where the reader will
     apply it.
   - The **horizontal checklist** fundamentals that aren't already covered by a vertical
     → fold the closely-related ones into a single "backend fundamentals woven through
     this project" doc rather than one tiny doc each. Skip anything purely mechanical.

Then **present the planned doc list** (concept → filename) before writing, and **skip
concepts already covered** by an existing file in `docs/` (report which you're skipping).
If the user narrowed scope in step 1, honor it.

## 3. Ground each concept in this project — spoiler-free

- Anchor to **this project's** concrete surface: the vertical's module name, the types
  and signatures the scaffold already exposes, the SPEC's Done-when criteria, the
  project's on-disk layout and Docker deps. Cite real files with **relative markdown
  links** (e.g. `[id_gen.rs](../src/id_gen.rs)`) — including the `todo!()` the reader
  will fill, pointed at as *the thing you're about to build*, never filled in.
- Teach the **general concept** first-principles (why it exists, the naive approach and
  how it breaks, the tradeoffs named in the SPEC — e.g. cache-aside vs write-through, or
  Snowflake vs UUIDv4 vs DB sequences). Use the SPEC's own "concept to internalize" and
  the `in the wild` framing where present.
- **Hard stop at the solution.** Do not write the algorithm, the data-structure choice
  that *is* the answer, or code that would drop into the `todo!()`. When you reach that
  line, name what the reader must decide and why it's the interesting part, then defer to
  `/hint` (graduated nudges) and `/quest` (guided build).

## 4. Verify every factual / computed claim

- Anything checkable, **check with a real tool before asserting it** — encodings, byte
  values, hashes (`sha256sum`), command output, arithmetic (the fan-out / bit-budget /
  throughput math). Never hand-wave a digest or a number from memory. If the SPEC's
  scaffold diverges from the ideal you're describing, say so honestly.

## 5. Write each doc — first principles, no assumed knowledge

Follow the house teaching style (concrete-scenario-first, derive-don't-declare, no
assumed background). Use the existing docs as the quality bar
(`projects/01-url-shortener/docs/`, `projects/06-object-store/docs/`). Each concept doc:

- A short blockquote intro: what this teaches, "no prior knowledge assumed", the SPEC
  vertical it prepares you for, and the code files it's anchored to.
- **"The one sentence to hold onto"** — the single core idea, up top.
- **The problem before the solution** — why the naive approach fails (a table of
  concrete failure modes works well), motivating the design space.
- **Concrete worked examples** — real keys/bytes/ids/paths traced through, in tables and
  ASCII diagrams, not prose alone.
- **The design space, not the answer** — lay out the tradeoffs the SPEC asks the reader
  to weigh; make the decision *visible* without making it *for* them.
- **A mental-model summary table** and a **"where you'll build this"** pointer to the
  vertical's module + `todo!()`, plus the Done-when criteria this doc unlocks.

## 6. Filenames, index & finish

- Write to `projects/NN-name/docs/<PP>-<kebab-concept>.md`, `<PP>` being the next
  zero-padded 2-digit prefix after the highest already in that `docs/` (start at `00`).
  Order the set so foundational concepts come first (usually V1 → Vn, fundamentals last).
- Report every path created (and any skipped), then a 2–3 line summary of what the set
  covers. Do **not** start implementing anything in `src/` — the whole point is that the
  reader writes the verticals themselves.
