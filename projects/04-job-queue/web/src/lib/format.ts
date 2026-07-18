// Small presentation helpers. Numbers on this dashboard are read at a glance while
// they move, so favour short, stable-width strings over precision.

export function fmtInt(n: number): string {
  if (!Number.isFinite(n)) return '—'
  return Math.round(n).toLocaleString()
}

export function fmtNum(n: number | undefined, digits = 1): string {
  if (n === undefined || !Number.isFinite(n)) return '—'
  return n.toFixed(digits)
}

/** A rate already in per-second units → "12.3/s". */
export function fmtRate(n: number | undefined): string {
  if (n === undefined || !Number.isFinite(n)) return '—'
  return `${n < 10 ? n.toFixed(1) : Math.round(n)}/s`
}

/** Milliseconds → "45 ms" or "1.2 s". */
export function fmtMs(ms: number | undefined): string {
  if (ms === undefined || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`
}

/** Seconds of lag/age → "0s", "3.2s", "1m 04s", "2h 05m". */
export function fmtDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0s'
  if (seconds < 10) return `${seconds.toFixed(1)}s`
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60)
    const s = Math.round(seconds % 60)
    return `${m}m ${String(s).padStart(2, '0')}s`
  }
  const h = Math.floor(seconds / 3600)
  const m = Math.round((seconds % 3600) / 60)
  return `${h}h ${String(m).padStart(2, '0')}m`
}

/** ISO timestamp → coarse "just now" / "5s ago" / "3m ago". */
export function timeAgo(iso: string): string {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return '—'
  const s = Math.max(0, (Date.now() - then) / 1000)
  if (s < 2) return 'just now'
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}
