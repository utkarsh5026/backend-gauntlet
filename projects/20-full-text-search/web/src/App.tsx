import { useCallback, useEffect, useRef, useState } from 'react'
import { KeyRound, Search as SearchIcon } from 'lucide-react'

import * as api from '@/api'
import type { EngineStats, SearchResponse } from '@/api'
import { IndexPanel } from '@/components/IndexPanel'
import { Results } from '@/components/Results'
import { SearchBar } from '@/components/SearchBar'
import { StatsBar } from '@/components/StatsBar'
import { Input } from '@/components/ui/input'

export default function App() {
  const [query, setQuery] = useState('')
  const [size, setSize] = useState(10)
  const [result, setResult] = useState<SearchResponse | null>(null)
  const [searched, setSearched] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [stats, setStats] = useState<EngineStats | null>(null)
  const [online, setOnline] = useState<boolean | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [apiKey, setApiKeyState] = useState('')

  // The query that produced the current results, so mutations can re-run it.
  const lastQuery = useRef('')

  const loadStats = useCallback(async () => {
    try {
      setStats(await api.stats())
      setOnline(true)
    } catch {
      setOnline(false)
    }
  }, [])

  useEffect(() => {
    void loadStats()
  }, [loadStats])

  // Fade the transient status note out after a few seconds.
  useEffect(() => {
    if (!note) return
    const t = setTimeout(() => setNote(null), 4000)
    return () => clearTimeout(t)
  }, [note])

  const runSearch = useCallback(
    async (q: string) => {
      const trimmed = q.trim()
      if (!trimmed) return
      setLoading(true)
      setError(null)
      try {
        const res = await api.search(trimmed, size)
        setResult(res)
        setSearched(true)
        lastQuery.current = trimmed
        setOnline(true)
      } catch (e) {
        setError(e instanceof api.ApiError ? e.message : String(e))
        setResult(null)
        setSearched(true)
      } finally {
        setLoading(false)
      }
    },
    [size],
  )

  const afterMutation = useCallback(
    (msg: string) => {
      setNote(msg)
      setError(null)
      void loadStats()
      if (lastQuery.current) void runSearch(lastQuery.current)
    },
    [loadStats, runSearch],
  )

  const onDelete = useCallback(
    async (id: string) => {
      try {
        await api.deleteDocument(id)
        await api.refresh()
        afterMutation(`Deleted “${id}”.`)
      } catch (e) {
        setError(e instanceof api.ApiError ? e.message : String(e))
      }
    },
    [afterMutation],
  )

  return (
    <div className="mx-auto min-h-screen max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
      {/* Header */}
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="bg-primary text-primary-foreground flex size-10 items-center justify-center rounded-lg">
            <SearchIcon className="size-5" />
          </div>
          <div>
            <h1 className="text-lg leading-tight font-semibold">Full-Text Search</h1>
            <p className="text-muted-foreground text-xs">BM25 over a sharded inverted index · project 20</p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="relative">
            <KeyRound className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2" />
            <Input
              type="password"
              value={apiKey}
              onChange={(e) => {
                setApiKeyState(e.target.value)
                api.setApiKey(e.target.value)
              }}
              placeholder="API key (writes)"
              className="h-9 w-40 pl-8 font-mono text-xs"
            />
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span
              className={
                'size-2 rounded-full ' +
                (online === null ? 'bg-muted-foreground' : online ? 'bg-success' : 'bg-destructive')
              }
            />
            <span className="text-muted-foreground">
              {online === null ? 'connecting' : online ? 'online' : 'offline'}
            </span>
          </div>
        </div>
      </header>

      {/* Search hero */}
      <div className="mb-4">
        <SearchBar
          query={query}
          size={size}
          loading={loading}
          onQueryChange={setQuery}
          onSizeChange={setSize}
          onSubmit={() => runSearch(query)}
        />
      </div>

      {note && (
        <div className="border-success/40 bg-success/10 text-success mb-4 rounded-lg border px-4 py-2 text-sm">
          {note}
        </div>
      )}

      {/* Body: results + admin sidebar */}
      <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
        <main className="min-w-0">
          <Results
            query={lastQuery.current}
            result={result}
            error={error}
            searched={searched}
            onDelete={onDelete}
          />
        </main>

        <aside className="flex flex-col gap-4">
          <StatsBar stats={stats} />
          <IndexPanel onChanged={afterMutation} onError={setError} />
        </aside>
      </div>
    </div>
  )
}
