---
description: Generate (or refresh) a project's README.md — a first-person, GitHub-beautiful story distilled from its SPEC, docs, research, benches, and code
argument-hint: <project, e.g. "06" or "06-object-store"; omit to infer from context>
---

Write the README for project: **$ARGUMENTS**

A README is the project's front door. It should make a stranger on GitHub feel
the *gravitas* of what was built here — not read like a generated file listing.
It is written in **my voice** (the repo owner's, first person): "I built…",
"I'm currently working on…", "next I want to…". Claude is the ghostwriter;
the story is mine.

## 1. Resolve the project

- Parse `$ARGUMENTS` for a project (`06`, `06-object-store`, …). If absent,
  infer from the files currently in play; if still ambiguous, ask.
- Target file is `projects/NN-name/README.md`. If one already exists, treat it
  as a **refresh**: preserve any hand-written passages that still ring true
  (especially personal anecdotes), update everything stale, and say in your
  report what you kept vs. rewrote.

## 2. Read everything first (no skimming)

Absorb the whole project before writing a word:

- **`SPEC.md`** — the status block, every `### Vn.` vertical, each "concept to
  internalize", the "Done when ALL true" boxes and their tick states, the
  horizontal checklist, the 🐉 boss fight, and any `## 🔬 From the field`
  backlog. Tick states are the ground truth for *done / in progress / future*.
- **`CONCEPTS.md`** and every file in **`docs/`** — these are the intellectual
  meat; mine them for the "why it's hard" and "what I learned" material.
- **`RESEARCH.md`** if present — how the real companies do it; great for
  framing ("S3 does X; my version does Y").
- **`bench/`** and any benchmark docs — **real numbers are the crown jewels.**
  Pull actual measured figures (RPS, p99, hit ratios) with their context.
  Never invent or round-trip numbers that aren't in the repo.
- **`src/`** (module list + `main.rs` wiring), **`tests/`**, **`migrations/`**,
  **`docker-compose.yml`**, **`makefile.py`**, and **`web/`** if present — to
  describe the architecture and the run/dev story accurately.
- `make status NN=<NN>` for the current progress snapshot.

Cross-check claims against code: a vertical only counts as "built" if its
boxes are ticked *and* the module isn't a `todo!()` shell.

## 3. Voice & honesty rules

- **First person, natural prose.** "I built the ID generator on a Snowflake
  layout because…" — not "This project implements…". Contractions welcome.
  Write like I'm walking a friend through the repo, proud but honest.
- **Three tenses, clearly felt:** what **I built** (past, with evidence),
  what **I'm building now** (the active vertical / in-flight boxes), and what
  **I want to do next** (unticked verticals, From-the-field items). Never
  present unfinished work as finished — the SPEC's tick states decide.
- Convey difficulty by *showing*, not adjectives: name the failure mode, the
  tradeoff, the number. "I had to make overwrite an atomic pointer flip so a
  reader never sees a torn object" beats "robust and performant".
- This is a learning repo — say so with pride. "Built from scratch, no
  `cargo add` for the interesting parts" is the flex.
- No hype-words (blazingly fast, production-ready, enterprise-grade), no
  spoiling SPEC solutions beyond what the code already reveals, no lying
  about numbers.

## 4. Structure — beautiful on GitHub, never boring

Use GitHub-flavored markdown's full toolkit, in service of readability:

- **Hero:** if the project has a logo image (look for `logo.png` / `logo.svg`
  in the project root or an `assets/` folder), lead with it:
  `<img src="logo.png" alt="<name> logo" width="200">` inside the centered
  block, above the title. Reference it by relative path — never copy, move, or
  regenerate the image. If no logo file exists, skip it silently (don't invent
  one, don't leave a broken link). Then an `<h1>` with the project's emoji +
  name, a one-line tagline in *italics* that captures the essence, then a
  short badge row
  (`img.shields.io` static badges: Rust, the key deps/infra, a
  `status: active|paused|done` badge from the SPEC status block). Center the
  hero with `<div align="center">`.
- **Opening story (2–3 paragraphs):** why I built this, what the hard problem
  is, and the one-sentence version of how it works. This is the hook — write
  it last, best.
- **A diagram:** a Mermaid `flowchart` (or `sequenceDiagram` where flow fits
  better) of the real architecture — actual module/service names from the
  code. GitHub renders Mermaid natively; keep it small enough to read.
- **"What I built" section:** one subsection or table row per *completed*
  vertical — the concept it forced me to internalize, the mechanism I chose,
  and a pointer into `src/` or `docs/`. Tables for enumerable facts, prose
  for the interesting parts.
- **"Where I am now":** the in-flight vertical(s), phrased as present tense —
  what works already, what's still open (mirror the unticked boxes without
  copying them verbatim).
- **Numbers section** (only if benches exist): a small table of measured
  results + one sentence of context each ("on my WSL2 box, hot tier…").
  Include the 🐉 boss fight as narrative: what the scenario is, whether the
  boss has fallen, and the proof.
- **"What's next":** future tense, my ambitions — unstarted verticals and the
  juiciest From-the-field items.
- **Run it:** the minimal honest path — `docker compose up -d`, `make dev`,
  `cargo run -p <crate>` — with the project-scoped host ports called out.
  Test with `make verify`. Keep this short; it's a learning repo, not a
  product install guide.
- **Deep dives:** link every `docs/*.md` with a one-line first-person hook
  each ("where I work out why ETags aren't checksums"), plus SPEC.md,
  CONCEPTS.md, RESEARCH.md.
- Sprinkle, don't drench: `<details>` blocks for long-tail content (full
  vertical table, bench methodology), `> [!NOTE]`/`> [!IMPORTANT]` alerts for
  the one or two things a reader must not miss, `---` rules between acts.
  Emoji as signposts on `##` headers only. Every embellishment must earn its
  place — a README that's all chrome is as boring as one that's all text.

## 5. What a README is *not*

- Not a second SPEC: never copy checkbox blocks or "Done when" lists in — the
  SPEC stays the single graded surface. The README *narrates*; it links to
  `SPEC.md` for the contract.
- Not counted by the tracker, but be safe anyway: use no `- [ ]` / `- [x]`
  markers at all in the README (prose and tables can express state better).
- Not a place for secrets, real `.env` values, or absolute local paths.

## 6. Verify & report

- Render-check your own output: balanced code fences, valid Mermaid syntax
  (no `(`/`)` inside node labels), tables aligned, all relative links resolve
  to files that exist (`docs/…`, `SPEC.md`, `src/…`).
- Confirm `make status NN=<NN>` output is unchanged by the edit.
- Report: the narrative arc you chose, which numbers you pulled from where,
  any claims you *downgraded* because code/ticks didn't back them, and (on a
  refresh) what hand-written material you preserved.
