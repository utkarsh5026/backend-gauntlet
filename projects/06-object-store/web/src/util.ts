/** Human-readable byte count (binary units, matching S3/disk conventions). */
export function fmtBytes(n: number): string {
  if (!Number.isFinite(n)) return '—'
  if (n < 1024) return `${n} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`
}

/** Best-effort local date formatting; falls back to the raw string. */
export function fmtDate(s: string): string {
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

/** Pull a readable message out of an unknown thrown value. */
export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message
  return String(e)
}
