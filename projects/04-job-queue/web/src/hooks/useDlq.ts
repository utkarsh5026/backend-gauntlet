import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchDlq, requeue, type Job } from '@/lib/api'

export interface UseDlq {
  jobs: Job[]
  loading: boolean
  error: string | null
  refresh: () => void
  requeueOne: (id: number) => Promise<void>
  busyId: number | null
}

/** Poll GET /dlq on a slow cadence (the DLQ changes rarely) and expose a
 * requeue action that optimistically drops the row on success. */
export function useDlq(intervalMs = 4000): UseDlq {
  const [jobs, setJobs] = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const inFlight = useRef(false)

  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const list = await fetchDlq(100)
      setJobs(list)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
      inFlight.current = false
    }
  }, [])

  const requeueOne = useCallback(
    async (id: number) => {
      setBusyId(id)
      try {
        await requeue(id)
        setJobs((js) => js.filter((j) => j.id !== id))
        setError(null)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setBusyId(null)
      }
    },
    [],
  )

  useEffect(() => {
    void refresh()
    const t = setInterval(() => void refresh(), intervalMs)
    return () => clearInterval(t)
  }, [refresh, intervalMs])

  return { jobs, loading, error, refresh: () => void refresh(), requeueOne, busyId }
}
