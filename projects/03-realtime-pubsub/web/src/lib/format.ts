// Tiny display formatters, kept out of components so the JSX stays readable.

export function fmtNum(n: number): string {
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`
  return `${(n / 1_000_000).toFixed(1)}M`
}

export function fmtRate(n: number): string {
  return `${fmtNum(Math.round(n))}/s`
}

export function fmtLatency(ms: number | null): string {
  if (ms === null) return '—'
  if (ms < 1) return '<1 ms'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

export function clockTime(at: number): string {
  const d = new Date(at)
  return d.toLocaleTimeString([], { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0')
}

export function shortTime(at: number): string {
  return new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

export function initials(name: string): string {
  const t = name.trim()
  return t ? t.slice(0, 2).toUpperCase() : '??'
}
