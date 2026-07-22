---
description: Generate (or refresh) a project's architecture diagrams — plain-language SVGs + an assets.json manifest the Pages site renders; incremental, regenerating only what the code changed
argument-hint: <project, e.g. "01" or "01-url-shortener"; omit to infer from context>
---

Create or refresh the visual assets for project: **$ARGUMENTS**

The goal: someone landing on the Pages site who has *never read the code* should
look at these diagrams and understand what the project is, how a request moves
through it, and why it's built that way. Diagrams are hand-authored SVGs in the
site's visual language, and every one ships with a plain-language description in
a manifest (`assets.json`) that the site reads. This command is **incremental**:
on re-runs it only touches diagrams whose underlying code actually changed, and
adds new ones only for genuinely new major features.

This command writes SVGs and JSON only. It **reads** Rust code but never
modifies it — the learning-repo rule is untouched.

## 1. Resolve the project

- Parse `$ARGUMENTS` (`01`, `01-url-shortener`, …). If absent, infer from files
  currently in play; if still ambiguous, ask.
- Asset home: `projects/NN-name/assets/` (create it if missing).
- Manifest: `projects/NN-name/assets/assets.json`.
- Site mirror: `site/public/assets/NN-name/` — kept as an exact copy (step 8).

## 2. Load the manifest first — decide mode

If `assets.json` exists this is a **refresh**, not a regeneration. For every
existing entry, use its `depicts` list and `sourceCommit` to find what changed:

```bash
git diff --name-only <entry.sourceCommit>..HEAD -- projects/NN-name/<each depicts path>
```

Then classify each entry — and say which bucket each landed in when you report:

- **keep** — no depicted file changed. Don't touch the SVG or the entry.
- **re-verified** — depicted files changed, but after re-reading them the
  diagram is still *accurate* (refactor, rename of internals it doesn't show,
  comment churn). Bump `sourceCommit` + `updatedAt`; leave the SVG alone.
- **regenerate** — the picture is now *wrong*: a component appeared/disappeared,
  a flow gained/lost a hop, storage or protocol changed, labels lie. Redraw it.
  Update the description too if the story changed.
- **retire** — the feature it depicted was removed. Delete the SVG and the
  entry (from both the project folder and the site mirror).

Never regenerate on a whim ("could be prettier") — churn makes diffs unreviewable.
Accuracy is the only trigger; beauty problems get fixed when accuracy forces a redraw.

Then scan for **new** diagram-worthy material (step 4's bar): a vertical that
has since been implemented, a new service in `docker-compose.yml`, a new module
wired into `main.rs`. Minor additions (a helper, a config knob, an extra
endpoint on an existing router) do **not** earn a diagram.

## 3. Read the project before drawing anything

Same discipline as `/readme` — absorb, then draw:

- **`SPEC.md`** — verticals, tick states, the boss fight. Tick states are the
  ground truth for what exists.
- **`src/`** — `main.rs` wiring, one pass over each module. A module that is
  still a `todo!()` shell **does not exist** for diagram purposes.
- **`docker-compose.yml`** — real infra topology (postgres/redis/nats + ports).
- **`docs/`, `CONCEPTS.md`** — the *why* behind the design; mine these for the
  plain-language descriptions.
- **`migrations/`** — actual tables, if a data-model diagram is warranted.

**Honesty rule:** diagrams depict what is *built*. A planned/unimplemented part
may appear only if it's visually unmistakable as planned — dashed outline,
muted color, literal `(planned)` in its label. Never draw aspiration as fact.

## 4. Choose the diagram set

Quality over coverage. Typical set is **2–5 diagrams**, in priority order:

1. **`system-overview`** (always, and always first): every process/service as a
   box, arrows for who talks to whom, one line inside each box saying what it's
   *for*. If a stranger sees one image, it's this one.
2. **`request-flow`**: the hot path, step by step, left to right — e.g. "what
   happens in the 20ms after you click a short link". Number the steps.
3. **One per implemented vertical that has real internal machinery** (an ID
   generator's bit layout, a cache's stampede gate, a WAL's record framing).
   Skip verticals whose picture would just restate the overview.
4. **`data-model`** only if the schema has a story (relationships, partitioning
   — not a single flat table).

What does **not** earn a diagram: config plumbing, error types, telemetry
boilerplate, anything whose diagram would be one box.

**Drafting with the user in the loop (optional):** when the user is present and
a diagram is new or being redrawn, sketch the proposed layout first with the
Excalidraw connector (`create_view`) and let them react — they can edit it
fullscreen, and their version is recoverable via the returned `checkpointId`.
Agree on the layout, then encode it as the site-language SVG. Skip this in
autonomous runs. Excalidraw output never lands in the repo — it is the
whiteboard, not the asset.

## 5. Plain-language rules (the point of all this)

The audience is a curious person on the frontend of the site, not a Rust
reviewer. For every diagram:

- It must answer **one question**, and its `title` *is* that question or its
  answer ("How a click becomes a stats row", not "Async ingestion pipeline").
- **Terse boxes, deep captions:** at most **2 short lines of prose per box** —
  the diagram is the skim layer. The full rationale lives in the manifest
  `description` bullets, which the site shows behind a `[+] why this matters`
  disclosure. Never wall-of-text a diagram; give the saved space back as
  whitespace between elements.
- Labels inside the SVG: short, concrete, jargon-free. "Remembers hot links so
  the DB isn't asked twice" beats "cache-aside layer". Port numbers and crate
  names only where they genuinely orient the reader.
- The manifest `description` is an **array of 3–5 short bullet points**, never a
  paragraph — the site renders each as its own bullet. Its whole job is to make
  the stakes legible to a curious **non-backend reader**: **name the problem
  this piece faces, then how the design solves it** — the trap first, the fix
  second. One idea per bullet, everyday words, short sentences; read it back and
  cut anything that sounds like a Rust reviewer wrote it. The `summary` stays
  one card-sized line (a plain string, not bullets).
- If a term can't be avoided (WAL, quorum), define it in the same bullet in
  plain words the first time it appears.

## 6. SVG style guide — the site's visual language

Hand-author the SVG. Match the Pages site exactly so images feel native to it:

- **Palette** (from `site/src/index.css` — the TUI "ink & instrument" system):
  background `#0e1311`, elevated panels `#141a17`, text `#d3ddd6`, muted text
  `#7f8d84`, accent teal `#5fb8a1` (dim `#3e7f70`), borders `#26302b`,
  ok `#79b878`, warn `#ceac63`, err `#c97f6f`. Teal is for emphasis — the hot
  path, the one box that matters. Most strokes are `#26302b`; if everything is
  teal, nothing is. Color is semantic: green = done/safe, amber = warning,
  red = failure/blocked.
- **Always paint an opaque `#0e1311` background rect** so the image also reads
  correctly on GitHub's white README background.
- Fonts: system stacks only — labels
  `ui-monospace, SFMono-Regular, Menlo, monospace` for names/ports,
  `system-ui, sans-serif` for prose captions. No webfonts (they won't load
  inside `<img>`).
- Real `<text>` elements, wrapped by hand — **no `<foreignObject>`** (GitHub
  strips it), no external images/CSS/scripts, nothing fetched.
- `viewBox` with a width around 960, height as needed (~16:10 for overviews,
  wide-and-short for flows). Generous spacing: boxes ≥ 12px padding, arrows
  never crossing text, ≥ 24px gutters. Rounded corners (`rx="8"`), 1.5px
  strokes, arrowheads via a `<marker>` def.
- Dashed stroke + muted text = planned/optional. Solid = built. Legend only if
  the diagram actually uses both.

## 7. Render and self-check every SVG (resvg)

Never ship an SVG you haven't seen. Rasterize each new/regenerated SVG to the
scratchpad and **look at the PNG** (Read it) before writing the manifest:

```bash
~/.local/bin/resvg --zoom 1.5 \
  --font-family "DejaVu Sans" --sans-serif-family "DejaVu Sans" \
  --monospace-family "DejaVu Sans Mono" \
  projects/NN-name/assets/<id>.svg <scratchpad>/<id>.png
```

- `resvg` lives at `~/.local/bin/resvg`. If it's missing (no-sudo box): download
  the prebuilt `resvg-linux-x86_64.tar.gz` from the latest `linebender/resvg`
  GitHub release into `~/.local/bin` and `chmod +x` it.
- The font flags matter: `system-ui`/`ui-monospace` aren't real font names here,
  so map the *generic* fallbacks to installed fonts (warnings about them are
  expected noise — add `2>/dev/null`). DejaVu runs slightly wider than the
  browser's system-ui, so if it fits in DejaVu it fits everywhere.
- Look for: text colliding with text or borders, content escaping its box,
  arrows crossing labels, a wrap that broke mid-thought. Fix the SVG and
  re-render until clean — the render is the reviewer, not the width arithmetic.
- Two gotchas the render catches that estimates miss: SVG collapses runs of
  spaces (never column-align with spaces — position a second `<text x=…>`
  instead), and long mono paths beside a prose column are the first collision.

## 8. Write the manifest + sync the site mirror

`assets.json` schema (the site's `AssetGallery` component types match this —
keep them in lockstep if you change it):

```json
{
  "project": "01-url-shortener",
  "updatedAt": "2026-07-19",
  "assets": [
    {
      "id": "system-overview",
      "file": "system-overview.svg",
      "kind": "architecture",
      "title": "What answers a click, and what it leans on",
      "summary": "The three moving parts: the axum server, Redis in front, Postgres behind.",
      "description": [
        "Every redirect is a read, so a handful of popular links get looked up thousands of times a second.",
        "Redis sits in front as a cache, so those repeat lookups are answered from memory and never touch the database.",
        "Postgres stays the source of truth — wipe the cache and it refills from Postgres, so a cache crash loses speed, never links."
      ],
      "depicts": ["src/main.rs", "src/routes.rs", "docker-compose.yml"],
      "spec": ["V1", "V2"],
      "sourceCommit": "8c64a52",
      "updatedAt": "2026-07-19"
    }
  ]
}
```

- `kind`: `architecture | request-flow | data-flow | internals | data-model | infra`.
- `description`: a JSON **array of 3–5 plain-language bullet strings** (see step
  5) — the problem this piece faces and how the design answers it, one idea per
  string. It is an array, not a paragraph; the site renders each string as a
  bullet. `summary` stays a single plain string.
- `depicts`: paths **relative to the project dir** — this is the staleness
  contract (step 2 diffs exactly these). List every file whose change could
  make the picture lie; don't pad it with files the diagram ignores.
- `spec`: which verticals it illustrates (`[]` fine for overview/infra).
- `sourceCommit`: short HEAD hash at (re)generation/verification time.
- Order entries by priority (overview first) — the site renders in order.

Then mirror: copy the entire `projects/NN-name/assets/` folder to
`site/public/assets/NN-name/` (delete files there that no longer exist in the
source folder). The site fetches
`{BASE_URL}assets/NN-name/assets.json` at runtime — no site rebuild data step.

## 9. Verify and report

- Confirm every `file` in the manifest exists in **both** locations, and no
  orphan SVGs sit in either.
- Validate the JSON parses and every entry has all required fields.
- Optional, when the user is around to review: publish an Artifact page — the
  final SVGs inlined at full width on `#0e1311`, each with its manifest title +
  description beneath — so the whole set can be reviewed rendered without
  waiting for a Pages deploy. Re-publish the same file path to keep one URL.
- Report as a short table: asset id → keep / re-verified / regenerated / new /
  retired, plus one line on why for anything that isn't "keep". Remind me the
  site picks changes up on next Pages deploy.
