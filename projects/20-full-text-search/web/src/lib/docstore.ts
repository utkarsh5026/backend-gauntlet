// A local record of the documents this console has added.
//
// Why this exists: the engine has no way to list or fetch a document. That is not
// an oversight — an inverted index maps *terms to documents*, so it can answer
// "which docs contain `bm25`?" instantly and "show me document 7" not at all. The
// only document text the API ever hands back is the stored field on a search hit
// (routes.py: /search), and that needs V2 + V5 built before it returns anything.
//
// So the browser keeps its own list of what it sent, persisted in localStorage so
// a page reload doesn't lose it. It is a client-side notebook, NOT a view of the
// index — a document added with curl won't appear here, and if you restart the
// engine before refreshing, entries here will name documents the engine no longer
// has. The UI labels it accordingly and offers a way to clear it.

import type { NewDocument } from '@/api'

const KEY = 'fts.console.documents'

export interface StoredDoc {
  /** External id — generated if the user didn't name it, so the row is always
   *  addressable for delete. */
  id: string
  text: string
  /** Which shard the engine routed it to, when we know. `POST /documents` returns
   *  it; `_bulk` reports only a count, so seeded docs have `null` until a search
   *  hit tells us. */
  shard: number | null
  addedAt: number
}

function read(): StoredDoc[] {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as StoredDoc[]) : []
  } catch {
    // A corrupt or unavailable store must not take the page down with it.
    return []
  }
}

function write(docs: StoredDoc[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(docs))
  } catch {
    // Private browsing, quota, disabled storage — the list just won't persist.
  }
}

export function allDocs(): StoredDoc[] {
  return read().sort((a, b) => b.addedAt - a.addedAt)
}

/** Record documents we just sent. Re-adding the same id overwrites, mirroring the
 *  engine, where indexing a document twice under one id supersedes the first. */
export function remember(docs: NewDocument[], shard: number | null = null): StoredDoc[] {
  const now = Date.now()
  const byId = new Map(read().map((d) => [d.id, d]))
  docs.forEach((doc, i) => {
    const id = doc.id?.trim() || `untitled-${now}-${i}`
    byId.set(id, { id, text: doc.text, shard, addedAt: now + i })
  })
  const next = [...byId.values()]
  write(next)
  return next
}

export function forget(id: string): StoredDoc[] {
  const next = read().filter((d) => d.id !== id)
  write(next)
  return next
}

export function forgetAll(): StoredDoc[] {
  write([])
  return []
}
