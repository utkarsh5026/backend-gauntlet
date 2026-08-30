# full-text-search — web console

A React + TypeScript + Tailwind + shadcn/ui console for the project-20 BM25
full-text search engine, laid out as the three steps the engine actually runs:
**add documents → make them searchable → search**. Two live counters at the top
show where your documents are (waiting in memory vs. written to disk), which is
the whole near-real-time story in one picture. Because the backend is a scaffold,
each step reports its own state — a `500` from an unbuilt vertical is rendered as
an amber *"you haven't written this yet"* card naming the file to open, not a red
error.

## Stack

- **Bun** for install/run/scripts (not npm/pnpm).
- **Vite** dev server + build, **React 18**, **Tailwind v4**, **shadcn/ui** (new-york).
- Dark theme by default (`<html class="dark">`).

## Run

The console talks to the search engine through a Vite proxy under `/api`, so the
browser stays same-origin (no CORS needed on the backend).

```bash
# 1. Start the search engine (project root). Default port 9200.
make run

# 2. Start the console (this folder).
bun install
bun run dev            # → http://localhost:5173
```

Point at a non-default backend with `SEARCH_URL=http://host:port bun run dev`.

## Using it

The page is one column, top to bottom — do the steps in order.

1. **Where your documents are right now.** Two boxes: *waiting in memory* and *on
   disk, searchable*. Adding documents makes the left number climb; refreshing
   moves it to the right. Nothing else on the page explains "buffered vs segment"
   as well as watching those numbers change.
2. **Step 1 — Add documents.** *Load 12 example documents* bulk-indexes a demo
   corpus about search internals, or paste your own text. It deliberately does
   **not** auto-refresh: the documents sit in memory, unfindable, until you do
   step 2 yourself. That gap is the lesson.
3. **Click any document** in the *Documents you added* list to read it in the
   right-hand panel — full text, plus the token stream the engine actually
   indexes (lowercased, stop words struck through). Search hits open the same
   panel. Escape closes it.
4. **Step 2 — Make them searchable.** One *Refresh* button, which flushes memory
   into a segment file on disk (V2).
5. **Step 3 — Search.** Try `inverted index`, `bm25 ranking`, or `merge segments`.
   Each hit leads with the document text, matched words highlighted, and carries a
   quiet footer with its relevance bar, BM25 score and which shard answered.
   Hover a hit to delete it (V4).
6. **More** (collapsed at the bottom) holds the per-shard table and segment
   merging — real, but not part of the main loop, so it stays out of the way.

Each step's header pill reads `working`, `not built yet · V2`, or `untested`.
On load the console probes `GET /search` once — a read, so it cannot change the
index — to fill in step 3's state. It never probes refresh: an empty-buffer
refresh returns early without touching the segment writer, so it would falsely
report a working V2.

## Why the document list is client-side

The engine has **no endpoint that lists or fetches a document** — and that is the
data structure talking, not a missing feature. An inverted index maps *terms to
documents*, so it answers "which docs contain `bm25`?" in one lookup and "show me
document 7" not at all. The only document text the API ever returns is the stored
field on a search hit.

So `src/lib/docstore.ts` keeps the console's own record of what it sent, in
`localStorage`. It is a browser-side notebook, not a view of the index: a document
indexed with `curl` will not appear in it, and restarting the engine before a
refresh leaves entries naming documents the engine no longer has. *Clear list*
resets it.

## How highlighting works

The backend returns each hit's stored `text` but **no highlight offsets**, so the
console recovers what to `<mark>` by re-running an *approximation* of the server
analyzer (`src/full_text_search/analyzer.py`) on the query — lowercase, split on word boundaries,
drop the same English stop-words — and wrapping matching words. See
`src/highlight.tsx`. If you add a stemmer server-side (a V1 stretch), teach the
highlighter the same stemmer or it will drift from what actually matched.

## Notes

- Write/admin routes (index, bulk, delete, refresh, force-merge) are slated to sit
  behind an API key once the security horizontal is built. The console sends
  `X-API-Key` from the header field when set; the backend ignores it until then.
- Scaffold reality: the backend raises `NotImplementedError` until you build
  V2–V5, so refresh and search return 500 until those verticals land. The console
  maps endpoint → vertical in `src/lib/stages.ts` and turns those 500s into the
  amber "not built yet" cards; everything is wired to the real endpoints and
  lights up as you implement them.
