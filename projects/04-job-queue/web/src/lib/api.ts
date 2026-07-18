// Thin client over the endpoints the job-queue already serves. Everything is a
// relative path so Vite's dev proxy forwards it to the Rust backend (see
// vite.config.ts) — no CORS, no backend changes.

/** The `jobs` row shape, mirroring `Job` in src/job.rs. */
export interface Job {
  id: number
  queue: string
  kind: string
  payload: unknown
  state: 'ready' | 'running' | 'done' | 'dead'
  attempts: number
  max_attempts: number
  run_at: string
  locked_until: string | null
  last_error: string | null
  created_at: string
}

/** Body for POST /jobs, mirroring `NewJob` in src/job.rs. */
export interface NewJob {
  queue: string
  kind: string
  payload?: unknown
  max_attempts?: number
  delay_secs?: number
}

const TOKEN_KEY = 'jq_token'

/** The enqueue bearer token (ENQUEUE_TOKEN) — kept client-side in localStorage,
 * sent only to the same-origin dev proxy. Empty string = auth disabled backend. */
export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* private mode — ignore */
  }
}

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { authorization: `Bearer ${token}` } : {}
}

async function asError(res: Response): Promise<Error> {
  let detail = ''
  try {
    detail = (await res.text()).slice(0, 300)
  } catch {
    /* ignore */
  }
  return new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail}` : ''}`)
}

/** Raw Prometheus exposition text from GET /metrics. */
export async function fetchMetricsText(signal?: AbortSignal): Promise<string> {
  const res = await fetch('/metrics', { signal })
  if (!res.ok) throw await asError(res)
  return res.text()
}

/** Liveness probe. Returns true iff GET /healthz answers 2xx. */
export async function ping(signal?: AbortSignal): Promise<boolean> {
  try {
    const res = await fetch('/healthz', { signal })
    return res.ok
  } catch {
    return false
  }
}

/** POST /jobs → new job id. Throws on 4xx/5xx (401 = bad/missing token). */
export async function enqueue(job: NewJob): Promise<number> {
  const res = await fetch('/jobs', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...authHeaders() },
    body: JSON.stringify(job),
  })
  if (!res.ok) throw await asError(res)
  const body = (await res.json()) as { id: number }
  return body.id
}

/** GET /dlq?limit= — the dead-letter list, newest-visible first per the server. */
export async function fetchDlq(limit = 50, signal?: AbortSignal): Promise<Job[]> {
  const res = await fetch(`/dlq?limit=${limit}`, { signal })
  if (!res.ok) throw await asError(res)
  return (await res.json()) as Job[]
}

/** POST /job/{id}/requeue — move a dead job back to ready. */
export async function requeue(id: number): Promise<Job> {
  const res = await fetch(`/job/${id}/requeue`, { method: 'POST', headers: authHeaders() })
  if (!res.ok) throw await asError(res)
  return (await res.json()) as Job
}
