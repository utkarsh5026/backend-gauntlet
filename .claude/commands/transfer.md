---
description: Map a project's from-scratch primitives to the real-world skills they grant — what building this buys you when using/operating the production systems it mirrors, and where to pursue next
argument-hint: <project, e.g. "06" or "06-object-store"; omit to infer from branch/IDE/cwd>
---

Analyze the project **$ARGUMENTS** and explain what building it buys the user in the
real world: the transferable skills, the production systems it demystifies, and the
concrete directions to pursue next.

This is a LEARNING repo (see CLAUDE.md). The user reimplements from scratch the
primitives they'd normally `cargo add` or rent as a managed service. The recurring,
legitimate doubt is: *"I'll never run my version in prod — so what was the point?"*
This command answers that, honestly and specifically, grounded in the code they
actually wrote. It is a **retrospective + forward map**, not a solution or a review.

## 1. Resolve the target project

- Parse `$ARGUMENTS` for a project (`06`, `06-object-store`, a name, or a file whose
  project is unambiguous). If absent, infer in this order: current IDE selection /
  files in play → git branch (e.g. `feat/06-*`) → cwd under `projects/NN-*`. If still
  ambiguous, ask which project.

## 2. Learn what was ACTUALLY built (not what the SPEC aspires to)

- Read the project's `SPEC.md`: the verticals (`### Vn`), each vertical's "concept to
  internalize", the horizontal checklist, and the boss fight.
- Read the real `src/` modules the verticals map to. **Distinguish built from
  unbuilt**: a `todo!()` body, an unchecked `- [ ]`, or a `[~]` from-the-field item is
  *not* an earned skill — don't credit the user for code they haven't written. Only
  map primitives that genuinely exist. Quickly confirm with a glance at the module
  (real logic, not a stub), and if useful `git log --oneline` for what's landed.
- Note the project's `docs/`, `RESEARCH.md`, and `bench/` if present — they reveal how
  deep the user actually went on each concept.

## 3. Build the mapping — the core of the output

For each **built** primitive/vertical, produce a tight entry with these four beats
(cite the real module with a relative markdown link, e.g. `[cdc.rs](src/cdc.rs)`):

- **Real-world mirror** — the production system(s) / managed service this *is* a
  from-scratch version of (name concrete ones: AWS/GCS/Azure services, Redis, Kafka,
  Ceph, Postgres, Git, Docker, a CDN — whatever fits). If several, name them.
- **Operational skill unlocked** — the thing the user can now *do* with the real
  system that a non-implementer can't: tune a specific knob, debug a specific class
  of failure, avoid a specific footgun, read a specific bill line. Be concrete and
  causal ("you'll never buffer a whole object into a Vec because you felt the OOM at
  the type level"), not generic ("you understand storage better").
- **Demystified decision / failure mode** — the AWS/vendor design choice, pricing
  tier, or consistency guarantee this makes obvious rather than magic. Tie it back to
  the exact thing they implemented.
- **Generalizes to** — where the *primitive* shows up far outside this project's
  domain (content-addressing → Git/Docker/Nix; erasure coding → RAID/Ceph/CDNs;
  backpressure → all streaming; consensus → etcd/Postgres-replication). This is what
  makes the lesson worth more than the one system.

Prefer a scannable structure (a short section per primitive, or a table when the
beats are terse). Ground every claim in code that exists — no invented features, no
crediting stubs.

## 4. Name the honest edges

State plainly what this project **does not** teach — the boundary of what transfers,
so the user's mental model has honest edges rather than false confidence. (E.g. a
single-node object store teaches node-level correctness but not planet-scale
placement / cross-region replication / metadata at exabyte scale.) Point at which
later roadmap project (or which real body of knowledge) covers the gap.

## 5. Pursue directions — make it actionable

The user's goal is to *pursue in that direction*. End with a concrete forward map:

- **Go read the real thing** — 2–4 specific primary sources now readable *because* of
  what they built: a named paper (Haystack, the Dynamo paper, the Azure LRC paper,
  the S3 consistency post), a specific vendor doc/RFC, a real codebase (MinIO, Ceph,
  redb) to go spelunk with informed eyes.
- **Experiments that cash in the knowledge** — small, concrete things to try against
  the *real* system that will land now that they've built the model (e.g. "point the
  AWS CLI at your server and diff a multipart ETag", "compute your self-host
  durability and compare to S3 One Zone's published number", "trace a SigV4 403").
- **The next rung** — which unbuilt vertical, horizontal checklist item, or
  from-the-field `[~]` in *this* SPEC would deepen the most valuable skill, and which
  later roadmap project builds on this foundation.

## 6. Tone & finish

Be direct, specific, and honest — this is a gut-check, so it must survive scrutiny.
Credit only what's real; name the boundaries; don't inflate a learning exercise into
a résumé line it can't support. Default to reporting in chat (like `/spec-review`).
Do not modify `src/` or implement anything. If the user says "save this", write it to
`projects/NN-name/docs/` as the next `<PP>-transferable-skills.md`.
