import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchMetricsText } from '@/lib/api'
import { extractSnapshot, parsePrometheus, type MetricSnapshot } from '@/lib/metrics'

/** A snapshot plus per-second rates derived from the previous scrape's counters. */
export interface DerivedSnapshot extends MetricSnapshot {
  enqueueRate: number
  completeRate: number
  retryRate: number
  deadRate: number
}

export type ConnState = 'connecting' | 'live' | 'error'

/** How many scrapes to retain for the time-series charts (≈ window = len × interval). */
const HISTORY_LEN = 180

function rate(curr: number, prev: number, dtSec: number): number {
  const d = curr - prev
  if (d < 0 || dtSec <= 0) return 0 // counter reset or no elapsed time
  return d / dtSec
}

function derive(snap: MetricSnapshot, prev: DerivedSnapshot | undefined): DerivedSnapshot {
  if (!prev) {
    return { ...snap, enqueueRate: 0, completeRate: 0, retryRate: 0, deadRate: 0 }
  }
  const dt = Math.max(0.2, (snap.t - prev.t) / 1000)
  return {
    ...snap,
    enqueueRate: rate(snap.enqueued, prev.enqueued, dt),
    completeRate: rate(snap.completed, prev.completed, dt),
    retryRate: rate(snap.retried, prev.retried, dt),
    deadRate: rate(snap.deadLettered, prev.deadLettered, dt),
  }
}

export interface UseMetrics {
  history: DerivedSnapshot[]
  latest: DerivedSnapshot | null
  conn: ConnState
  error: string | null
  intervalMs: number
  setIntervalMs: (ms: number) => void
  paused: boolean
  setPaused: (p: boolean) => void
}

/** Poll GET /metrics on an interval, parse it, and keep a rolling history with
 * derived rates. The dashboard's single source of live data. */
export function useMetrics(initialInterval = 1000): UseMetrics {
  const [history, setHistory] = useState<DerivedSnapshot[]>([])
  const [conn, setConn] = useState<ConnState>('connecting')
  const [error, setError] = useState<string | null>(null)
  const [intervalMs, setIntervalMs] = useState(initialInterval)
  const [paused, setPaused] = useState(false)

  const inFlight = useRef(false)

  const tick = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), 4000)
    try {
      const text = await fetchMetricsText(ctrl.signal)
      const snap = extractSnapshot(parsePrometheus(text), Date.now())
      setHistory((h) => {
        const next = [...h, derive(snap, h[h.length - 1])]
        return next.length > HISTORY_LEN ? next.slice(next.length - HISTORY_LEN) : next
      })
      setConn('live')
      setError(null)
    } catch (e) {
      if (!ctrl.signal.aborted || e instanceof Error) {
        setConn('error')
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      clearTimeout(timer)
      inFlight.current = false
    }
  }, [])

  useEffect(() => {
    if (paused) return
    let cancelled = false
    const run = () => {
      if (!cancelled) void tick()
    }
    run()
    const id = setInterval(run, intervalMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [tick, intervalMs, paused])

  return {
    history,
    latest: history.length ? history[history.length - 1] : null,
    conn: paused ? 'connecting' : conn,
    error,
    intervalMs,
    setIntervalMs,
    paused,
    setPaused,
  }
}
