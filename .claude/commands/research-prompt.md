---
description: Generate a copy-paste deep-research prompt for a project — how the real world builds this thing today and what's being invented next — shaped to drop into RESEARCH.md and feed /harvest
argument-hint: <project, e.g. "13" or "13-live-ingest"; omit to infer from branch/IDE/cwd>
---

Produce a **ready-to-paste deep-research prompt** for the project **$ARGUMENTS**.

This is a LEARNING repo (see CLAUDE.md). Each project reimplements from scratch the
primitives you'd normally `cargo add` or rent as a managed service. Before (or
alongside) building, the user wants to understand the **real world**: how production
systems and real companies solve this exact problem today, and what's being invented
right now on the frontier. That understanding gets captured in the project's
`RESEARCH.md`, which `/harvest` then distills into the SPEC's "From the field"
adoption backlog.

You are **not** doing the research yourself and **not** writing `RESEARCH.md`. Your
job is to emit **one self-contained prompt**, grounded in what *this* project actually
builds, that the user copies into a deep-research tool (Claude/ChatGPT/Gemini deep
research, etc.). The tool's output is what becomes `RESEARCH.md`. So the prompt must
carry all the project-specific context a general research tool needs — it can't see
this repo.

## 1. Resolve the target project

- Parse `$ARGUMENTS` for a project (`13`, `13-live-ingest`, a name, or a file whose
  project is unambiguous). If absent, infer in this order: current IDE selection /
  files in play → git branch (e.g. `feat/13-*`) → cwd under `projects/NN-*`. If still
  ambiguous, ask which project.

## 2. Learn what this project actually is

Read enough to ground the prompt in *this* project's real subject matter — don't
research the domain yourself, just extract what the project is about:

- `SPEC.md` — the opening framing paragraph, each vertical (`### Vn.` + its "concept
  to internalize"), the horizontal checklist, and the boss fight. These name the exact
  protocols, algorithms, data structures, and real-world systems in scope.
- `CONCEPTS.md`, `README.md`, and `docs/` if present — the domain vocabulary and the
  named production systems the project mirrors.
- A quick skim of `src/` module names and the roadmap entry in the root `README.md`
  to pin down the real-world category (e.g. "live video ingest & low-latency
  delivery", "content-addressed object storage", "durable job queue").

From this, distill (for your own use, to fill the prompt):
- The **one-line domain** and the **specific technologies** in scope (protocols,
  formats, algorithms — e.g. RTMP, AMF0, CMAF fMP4, LL-HLS blocking reload).
- The **real-world systems / companies** this is a from-scratch version of (name
  concrete ones: nginx-rtmp, Mux, Cloudflare Stream, YouTube Live, MinIO, Kafka…).
- The **hard sub-problems** the verticals fixate on — those become the research
  tool's must-cover topics.

## 3. Emit the prompt — the deliverable

Output **exactly one fenced code block** (```text) containing the full prompt, so the
user can one-click copy it. Before the block, a one-line "Paste this into your
deep-research tool of choice." After it, the two follow-up lines from §4. Nothing
else — no preamble essay.

The prompt you generate must:

- **Set the role & goal:** a systems engineer's deep dive into `<domain>` — how it
  works from first principles, **how production systems and real companies build it
  today**, and **what's being invented on the frontier (2025–2026)**. State that the
  reader is implementing the core primitives from scratch in Rust to learn, so the
  research should favor *mechanisms, invariants, tradeoffs, and hard-won operational
  lessons* over marketing or product tours.
- **Name the concrete scope** you distilled in §2 — the specific protocols/formats/
  algorithms and the specific real systems to compare (`nginx-rtmp` vs `Mux` vs
  `Cloudflare Stream` …). This specificity is what makes the output useful; a generic
  "research live streaming" prompt is worthless. Tell it to prefer primary sources
  (papers, RFCs, engineering blogs, real codebases) and to cite them inline.
- **Demand three lenses on every major topic:** (a) the fundamental mechanism, (b)
  how it's done in production at scale + the failure modes and knobs that bite, (c)
  the emerging / frontier approach and why it's better. The user explicitly wants
  both "how the real world handles it now" and "what innovations are happening" — bake
  both in.
- **Mandate the output structure below**, so the result drops straight into
  `RESEARCH.md` and `/harvest` can consume it. Adjust the Detail section list to the
  project's actual subtopics (6–12 sections), but keep the top-level skeleton:

  ```
  # <Domain> Internals: A Systems Engineer's Deep Dive

  ## TL;DR                — 3–5 sentences
  ## Key Findings         — ~8–12 punchy bullets, the stealable insights
  ## Details
  ### 1. <fundamentals>
  ### 2. …                — one ### per major subtopic, tuned to THIS project
  ### N. Advanced & emerging topics (2025–2026)   — the frontier, required
  ## Recommendations      — what a from-scratch builder at learning scale should
                            actually adopt vs skip, and why
  ## Caveats              — where the research is uncertain / rapidly changing
  ```

- **Ask for adoptable specifics**, phrased so `/harvest` has raw material: concrete
  mechanisms, invariants, wire/format details, correctness & testing practices, real
  numbers (latency/throughput/limits) — not vendor pricing or product history.
- **Be self-contained:** spell out any project-specific term the research tool won't
  know from the repo. Assume it has web access but zero knowledge of this codebase.

## 4. Finish

After the code block, add exactly two lines:
- where to save the tool's output — `projects/NN-name/RESEARCH.md`;
- that `/harvest NN` then distills it into the SPEC's "From the field" backlog.

Do not modify any project files, do not create `RESEARCH.md`, do not run the research
yourself. Your entire output is the prompt block plus those two lines.
