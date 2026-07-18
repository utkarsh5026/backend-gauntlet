// Parse the Prometheus text-exposition format the job-queue serves at GET /metrics
// and fold it into a typed snapshot. This is the whole "data source" for the
// dashboard — the backend is unchanged; we read exactly the metrics the SPEC's
// observability checklist asks you to emit (see src/metrics.rs).

/** One parsed sample line: `name{labels} value`. */
export interface Sample {
  name: string
  labels: Record<string, string>
  value: number
}

// Metric names — must match the `pub const` strings in src/metrics.rs.
const M = {
  ready: 'job_queue_ready_depth',
  running: 'job_queue_running_depth',
  dlq: 'job_queue_dlq_depth',
  lag: 'job_queue_oldest_ready_age_seconds',
  enqueued: 'job_queue_enqueued_total',
  completed: 'job_queue_completed_total',
  retried: 'job_queue_retried_total',
  deadLettered: 'job_queue_dead_lettered_total',
  leasesReaped: 'job_queue_leases_reaped_total',
  claimsEmpty: 'job_queue_claims_empty_total',
  exec: 'job_queue_execution_seconds',
  e2e: 'job_queue_end_to_end_latency_seconds',
} as const

const LABEL_RE = /([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"/g
const LINE_RE = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(.+?)(?:\s+\d+)?$/

function parseValue(raw: string): number {
  if (raw === '+Inf') return Infinity
  if (raw === '-Inf') return -Infinity
  if (raw === 'NaN') return NaN
  return Number(raw)
}

function parseLabels(block: string | undefined): Record<string, string> {
  const labels: Record<string, string> = {}
  if (!block) return labels
  let m: RegExpExecArray | null
  LABEL_RE.lastIndex = 0
  while ((m = LABEL_RE.exec(block)) !== null) {
    labels[m[1]] = m[2].replace(/\\"/g, '"').replace(/\\\\/g, '\\').replace(/\\n/g, '\n')
  }
  return labels
}

/** Parse a full Prometheus exposition body into flat samples (comments dropped). */
export function parsePrometheus(text: string): Sample[] {
  const out: Sample[] = []
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed === '' || trimmed.startsWith('#')) continue
    const m = LINE_RE.exec(trimmed)
    if (!m) continue
    out.push({ name: m[1], labels: parseLabels(m[2]), value: parseValue(m[3]) })
  }
  return out
}

const seriesOf = (s: Sample[], name: string) => s.filter((x) => x.name === name)

/** Sum a metric across all its label sets (e.g. total across queues). */
function sum(s: Sample[], name: string): number {
  return seriesOf(s, name).reduce((a, x) => a + (Number.isFinite(x.value) ? x.value : 0), 0)
}

/** Max across label sets — the right reducer for the lag gauge (not a sum). */
function max(s: Sample[], name: string): number {
  let hi = 0
  for (const x of seriesOf(s, name)) if (Number.isFinite(x.value) && x.value > hi) hi = x.value
  return hi
}

/** Distinct `queue` label values seen across the depth gauges. */
function queueNames(s: Sample[]): string[] {
  const set = new Set<string>()
  for (const name of [M.ready, M.running, M.dlq]) {
    for (const x of seriesOf(s, name)) if (x.labels.queue) set.add(x.labels.queue)
  }
  return [...set].sort()
}

/** A quantile in seconds for a metrics-rs histogram, however it renders.
 *
 * `metrics-exporter-prometheus` emits histograms either as a *summary*
 * (`name{quantile="0.99"}`) or, if buckets are configured, as classic
 * `name_bucket{le=…}`. We read the summary form when present, else interpolate
 * from cumulative buckets, else fall back to `undefined`. */
function quantileSeconds(s: Sample[], base: string, q: number): number | undefined {
  const quant = seriesOf(s, base).filter((x) => x.labels.quantile !== undefined)
  if (quant.length) {
    let best: Sample | undefined
    let bestGap = Infinity
    for (const x of quant) {
      const gap = Math.abs(Number(x.labels.quantile) - q)
      if (gap < bestGap && Number.isFinite(x.value)) {
        bestGap = gap
        best = x
      }
    }
    return best?.value
  }

  const buckets = seriesOf(s, `${base}_bucket`)
    .filter((x) => x.labels.le !== undefined)
    .map((x) => ({ le: parseValue(x.labels.le), count: x.value }))
    .sort((a, b) => a.le - b.le)
  const total = sum(s, `${base}_count`)
  if (!buckets.length || total <= 0) return undefined

  const target = q * total
  let prevLe = 0
  let prevCount = 0
  for (const b of buckets) {
    if (b.count >= target) {
      if (b.le === Infinity) return prevLe
      const span = b.count - prevCount
      const frac = span > 0 ? (target - prevCount) / span : 0
      return prevLe + frac * (b.le - prevLe)
    }
    prevLe = b.le
    prevCount = b.count
  }
  return buckets[buckets.length - 1]?.le
}

function avgMs(s: Sample[], base: string): number | undefined {
  const count = sum(s, `${base}_count`)
  const total = sum(s, `${base}_sum`)
  return count > 0 ? (total / count) * 1000 : undefined
}

const toMs = (v: number | undefined) => (v === undefined ? undefined : v * 1000)

/** A single scrape, reduced to the numbers the dashboard renders. */
export interface MetricSnapshot {
  t: number
  ready: number
  running: number
  dlq: number
  lagSeconds: number
  enqueued: number
  completed: number
  retried: number
  deadLettered: number
  leasesReaped: number
  claimsEmpty: number
  execAvgMs?: number
  execP50Ms?: number
  execP99Ms?: number
  e2eP50Ms?: number
  e2eP99Ms?: number
  queues: string[]
  /** Any `job_queue_*` series at all — false on a just-started server. */
  hasAny: boolean
  /** Depth gauges present — they only publish when RUN_WORKERS=true. */
  hasGauges: boolean
}

export function extractSnapshot(samples: Sample[], t: number): MetricSnapshot {
  const hasGauges = [M.ready, M.running, M.dlq].some((n) => seriesOf(samples, n).length > 0)
  const hasAny = samples.some((x) => x.name.startsWith('job_queue_'))
  return {
    t,
    ready: sum(samples, M.ready),
    running: sum(samples, M.running),
    dlq: sum(samples, M.dlq),
    lagSeconds: max(samples, M.lag),
    enqueued: sum(samples, M.enqueued),
    completed: sum(samples, M.completed),
    retried: sum(samples, M.retried),
    deadLettered: sum(samples, M.deadLettered),
    leasesReaped: sum(samples, M.leasesReaped),
    claimsEmpty: sum(samples, M.claimsEmpty),
    execAvgMs: avgMs(samples, M.exec),
    execP50Ms: toMs(quantileSeconds(samples, M.exec, 0.5)),
    execP99Ms: toMs(quantileSeconds(samples, M.exec, 0.99)),
    e2eP50Ms: toMs(quantileSeconds(samples, M.e2e, 0.5)),
    e2eP99Ms: toMs(quantileSeconds(samples, M.e2e, 0.99)),
    queues: queueNames(samples),
    hasAny,
    hasGauges,
  }
}
