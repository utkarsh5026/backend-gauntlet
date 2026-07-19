// Thin client for the project-20 full-text search HTTP API.
//
// All requests go through the Vite proxy prefix `/api` (see vite.config.ts),
// which strips the prefix and forwards to the Rust backend (default :9200). That
// keeps the browser same-origin, so no CORS layer is needed on the backend.
//
// The endpoints map 1:1 onto src/routes.rs:
//   GET    /healthz                          liveness
//   GET    /search?q=&size=                  rank documents (public)   → SearchResponse
//   POST   /documents        {id?, text}     index one doc (V1)        → { shard, doc_id }
//   POST   /_bulk            NDJSON body      index many (V2 bulk)      → { indexed }
//   DELETE /documents/{id}                    tombstone by external id (V4)
//   POST   /_refresh                          make buffered docs searchable (V2)
//   POST   /_forcemerge                       compact segments, drop tombstones (V4)
//   GET    /_stats                            per-shard + aggregate stats
//
// Write/admin routes are gated behind an API key once the security horizontal is
// built (see the TODOs in routes.rs). The backend does not check it yet, but we
// send `X-API-Key` when one is configured so the UI keeps working after it lands.

export const BASE = '/api'

// ── Response shapes (mirror src/doc.rs + src/shard.rs serde) ──────────────────

/** One ranked hit. `doc_id` is shard-local, hence the `shard` tag. `id`/`text`
 *  are the stored fields — present only when the segment kept them. */
export interface SearchHit {
  shard: number
  doc_id: number
  score: number
  id?: string
  text?: string
}

export interface SearchResponse {
  took_ms: number
  total: number
  hits: SearchHit[]
}

export interface ShardStats {
  shard: number
  segments: number
  buffered: number
  doc_count: number
  deleted: number
}

export interface EngineStats {
  shard_count: number
  total_docs: number
  total_segments: number
  total_buffered: number
  shards: ShardStats[]
}

export interface NewDocument {
  id?: string
  text: string
}

export class ApiError extends Error {
  status: number
  body: string
  constructor(status: number, body: string) {
    super(status === 0 ? body : `HTTP ${status}${body ? ` — ${body}` : ''}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

// The API key lives in module state so every write helper can attach it without
// threading it through each call. Set from the UI (see App.tsx).
let apiKey = ''
export function setApiKey(key: string): void {
  apiKey = key.trim()
}

function writeHeaders(base: Record<string, string> = {}): Record<string, string> {
  return apiKey ? { ...base, 'X-API-Key': apiKey } : base
}

async function expectOk(res: Response): Promise<Response> {
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, body.slice(0, 500))
  }
  return res
}

async function fetchOk(method: string, url: string, init?: RequestInit): Promise<Response> {
  let res: Response
  try {
    res = await fetch(url, { method, ...init })
  } catch {
    throw new ApiError(0, 'network error — is the search engine running? (PORT=9200 cargo run -p full-text-search)')
  }
  return expectOk(res)
}

// ── Read paths (public) ───────────────────────────────────────────────────────

export async function health(): Promise<boolean> {
  try {
    const res = await fetch(`${BASE}/healthz`)
    return res.ok
  } catch {
    return false
  }
}

export async function search(q: string, size = 10): Promise<SearchResponse> {
  const params = new URLSearchParams({ q, size: String(size) })
  const res = await fetchOk('GET', `${BASE}/search?${params}`)
  return res.json()
}

export async function stats(): Promise<EngineStats> {
  const res = await fetchOk('GET', `${BASE}/_stats`)
  return res.json()
}

// ── Write / admin paths (API-key gated once security is built) ─────────────────

export async function indexDocument(doc: NewDocument): Promise<{ shard: number; doc_id: number }> {
  const res = await fetchOk('POST', `${BASE}/documents`, {
    headers: writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(doc),
  })
  return res.json()
}

/** Bulk index as NDJSON — one JSON document per line, like Elasticsearch `_bulk`. */
export async function bulk(docs: NewDocument[]): Promise<{ indexed: number }> {
  const ndjson = docs.map((d) => JSON.stringify(d)).join('\n')
  const res = await fetchOk('POST', `${BASE}/_bulk`, {
    headers: writeHeaders({ 'Content-Type': 'application/x-ndjson' }),
    body: ndjson,
  })
  return res.json()
}

export async function deleteDocument(id: string): Promise<void> {
  await fetchOk('DELETE', `${BASE}/documents/${encodeURIComponent(id)}`, {
    headers: writeHeaders(),
  })
}

export async function refresh(): Promise<{ refreshed: number }> {
  const res = await fetchOk('POST', `${BASE}/_refresh`, { headers: writeHeaders() })
  return res.json()
}

export async function forceMerge(): Promise<{ merged_segments: number }> {
  const res = await fetchOk('POST', `${BASE}/_forcemerge`, { headers: writeHeaders() })
  return res.json()
}
