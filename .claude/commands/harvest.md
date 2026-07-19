---
description: Distill a project's RESEARCH.md into an ungraded "From the field" adoption backlog inside its SPEC.md
argument-hint: <project, e.g. "06" or "06-object-store"; omit to infer from context>
---

Harvest research into the SPEC adoption backlog for project: **$ARGUMENTS**

A project's `RESEARCH.md` (how the real companies build this thing) is full of
ideas worth stealing. Distill them into a tick-list section inside `SPEC.md` so
adoption is trackable — **without** touching the graded surface `tools/status.py`
counts.

## 1. Resolve the project

- Parse `$ARGUMENTS` for a project (`06`, `06-object-store`, …). If absent, infer
  from the files currently in play; if still ambiguous, ask.
- The project **must** already have `projects/NN-name/RESEARCH.md`. If it doesn't,
  stop and list the projects that do. RESEARCH.md is **read-only input** to this
  command — never create, edit, or reformat it.

## 2. Analyze deeply

- Read the **entire** RESEARCH.md — TL;DR, Key Findings, every Part under Details,
  Recommendations, Caveats — not just the summary sections. The best adoptable
  ideas usually hide mid-Part, not in the TL;DR.
- Read `SPEC.md` too: every vertical's "Done when ALL true" block, the horizontal
  checklist, and the boss fight. Anything already graded there is **excluded** —
  this backlog is for *extras*: industry techniques beyond the SPEC's contract.
- Select ideas genuinely adoptable in *this* project at its scale (a learning
  project, not AWS): concrete mechanisms, invariants, correctness/testing
  practices, API semantics. Skip vendor trivia, pricing, product history.
- Sweep the project's `src/` and `tests/`: an idea that has already landed enters
  the list pre-ticked.

## 3. Item format

One line per idea, phrased as an **observable outcome** (house acceptance-criteria
style — what you'd see working, not the steps to do it), each with a pointer back
to where it came from:

```markdown
- [~] Overwrite of an existing key is an atomic pointer flip — a reader never
  sees a torn object *(→ RESEARCH.md §Part 2)*
```

Be **comprehensive, not curated-short**: harvest every genuinely adoptable idea
in the research doc — a dense RESEARCH.md typically yields **15–25 items**. What
keeps a long list workable is structure, not omission: group items under themed
`###` subheadings (e.g. *API & protocol extras*, *Storage-engine labs*,
*Correctness practice*), ordered roughly quick-wins → ambitious inside each
group. Only reject an idea because it's genuinely inapplicable (needs a fleet /
multi-node / vendor scale), never to keep the list short.

## 4. Section format & the marker rule (critical)

`tools/status.py` counts every `- [ ]` / `- [x]` in SPEC.md toward the project's
progress bars. This backlog must **not** be counted, so it uses different markers:
**`- [~]` = open, `- [✔]` = adopted.** Never use `[ ]`/`[x]` in this section.
Detail view (`make NN`) shows them under **FROM THE FIELD** as an ungraded
meter — still excluded from progress bars.

Append it as the **last `## ` section** of SPEC.md (never inside a `### Vn.`
vertical span):

```markdown
## 🔬 From the field

<!-- Adoption backlog distilled from RESEARCH.md by /harvest. NOT graded:
     [~] = open, [✔] = adopted — not counted toward graded progress;
     shown under FROM THE FIELD in status detail.
     Tick a box when the idea has actually landed in this project. -->

- [~] … *(→ RESEARCH.md §Part N)*
```

## 5. Re-runs merge, never clobber

If the section already exists:

- Keep every existing line and its tick state. **Never flip `[✔]` back to `[~]`**,
  and never delete or reword a line — the user may have edited it by hand.
- Append only genuinely new items, deduping by meaning, not exact wording.
- Flip `[~]` → `[✔]` only when real code/tests prove the idea landed, and cite the
  proving file for each flip in your report.

## 6. Verify & report

- Run `make status NN=<NN>` **before and after** the edit: every `[done/total]`
  count must be identical. If any number moved, a counted marker slipped in — fix
  it before finishing.
- Report: items added / already present / flipped (with the proving file), and
  confirm the tracker counts are unchanged.
