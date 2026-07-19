# full-text-search — web console

A React + TypeScript + Tailwind + shadcn/ui search console for the project-20 BM25
full-text search engine. A search box with **client-highlighted BM25 results** — the
demo-day artifact — plus an index/admin panel so you can fill the index and watch
near-real-time refresh and segment merging.

## Stack

- **Bun** for install/run/scripts (not npm/pnpm).
- **Vite** dev server + build, **React 18**, **Tailwind v4**, **shadcn/ui** (new-york).
- Dark theme by default (`<html class="dark">`).

## Run

The console talks to the Rust backend through a Vite proxy under `/api`, so the
browser stays same-origin (no CORS needed on the backend).

```bash
# 1. Start the search engine (project root). Default port 9200.
cargo run -p full-text-search

# 2. Start the console (this folder).
bun install
bun run dev            # → http://localhost:5173
```

Point at a non-default backend with `SEARCH_URL=http://host:port bun run dev`.

## Using it

1. **Seed sample corpus** (right panel) to bulk-index ~12 demo documents about
   search internals, then it auto-refreshes so they're searchable.
2. Search for `inverted index`, `bm25 ranking`, `rust async`, or `merge segments`.
   Results are ranked by BM25 score (relevance bar + numeric score) with matched
   query terms highlighted in each snippet.
3. Index your own documents, delete a hit (hover → trash), or run **refresh** /
   **force-merge** and watch the stats bar update.

## How highlighting works

The backend returns each hit's stored `text` but **no highlight offsets**, so the
console recovers what to `<mark>` by re-running an *approximation* of the server
analyzer (`src/analyzer.rs`) on the query — lowercase, split on word boundaries,
drop the same English stop-words — and wrapping matching words. See
`src/highlight.tsx`. If you add a stemmer server-side (a V1 stretch), teach the
highlighter the same stemmer or it will drift from what actually matched.

## Notes

- Write/admin routes (index, bulk, delete, refresh, force-merge) are slated to sit
  behind an API key once the security horizontal is built. The console sends
  `X-API-Key` from the header field when set; the backend ignores it until then.
- Scaffold reality: the backend's `search`/index paths hit `todo!()` until you build
  V1–V5, so a live search will 500 (a panic) until those verticals land. Everything
  in the UI is wired to the real endpoints and will light up as you implement them.
