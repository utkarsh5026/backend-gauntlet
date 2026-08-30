import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, Layers, RefreshCw, Search as SearchIcon } from 'lucide-react'

import * as api from '@/api'
import type { EngineStats, SearchResponse } from '@/api'
import { AddDocuments } from '@/components/AddDocuments'
import { DocumentDrawer } from '@/components/DocumentDrawer'
import { DocumentList } from '@/components/DocumentList'
import { Pipeline } from '@/components/Pipeline'
import { Results } from '@/components/Results'
import { Stage } from '@/components/Stage'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { allDocs, forget, forgetAll, remember, type StoredDoc } from '@/lib/docstore'
import { classify, type StageId, type Status } from '@/lib/stages'

/** The console is one vertical column that follows the loop the engine actually
 *  runs: add documents → make them searchable → search. The old two-column
 *  layout put search first and hid indexing in a sidebar, so the order you had
 *  to do things in was nowhere on the page. */
export default function App() {
  const [stats, setStats] = useState<EngineStats | null>(null)
  const [online, setOnline] = useState<boolean | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<Record<StageId, Status>>({
    add: 'unknown',
    refresh: 'unknown',
    search: 'unknown',
    delete: 'unknown',
    merge: 'unknown',
  })

  const [query, setQuery] = useState('')
  const [result, setResult] = useState<SearchResponse | null>(null)
  const [searching, setSearching] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const lastQuery = useRef('')

  // The console's own record of what it added. The engine has no endpoint that
  // lists documents, so without this there is nothing to click on.
  const [docs, setDocs] = useState<StoredDoc[]>(() => allDocs())
  const [openDoc, setOpenDoc] = useState<StoredDoc | null>(null)

  const mark = useCallback((id: StageId, s: Status) => {
    setStatus((prev) => (prev[id] === s ? prev : { ...prev, [id]: s }))
  }, [])

  /** Turn a thrown error into either a stage status or a red banner. A 500 from
   *  an unwritten vertical is expected and handled by the stage card itself, so
   *  it must never also raise a scary banner. */
  const handleFail = useCallback(
    (id: StageId) => (e: unknown) => {
      if (e instanceof api.ApiError) {
        const s = classify(id, e.status)
        mark(id, s)
        setError(s === 'notBuilt' ? null : e.message)
      } else {
        mark(id, 'broken')
        setError(String(e))
      }
    },
    [mark],
  )

  const loadStats = useCallback(async () => {
    try {
      setStats(await api.stats())
      setOnline(true)
    } catch {
      setOnline(false)
    }
  }, [])

  // On load, probe search once. It is a read — it cannot change the index — so
  // it is safe to fire unasked, and it lets the page say up front whether search
  // works. Refresh is NOT probed: an empty-buffer refresh returns early without
  // touching the segment writer (index.py), so it would succeed and report a
  // working V2 that isn't there.
  useEffect(() => {
    void (async () => {
      await loadStats()
      try {
        await api.search('probe', 1)
        mark('search', 'works')
      } catch (e) {
        if (e instanceof api.ApiError) mark('search', classify('search', e.status))
      }
    })()
  }, [loadStats, mark])

  useEffect(() => {
    if (!note) return
    const t = setTimeout(() => setNote(null), 5000)
    return () => clearTimeout(t)
  }, [note])

  const runSearch = useCallback(
    async (q: string) => {
      const trimmed = q.trim()
      if (!trimmed) return
      setSearching(true)
      setError(null)
      try {
        const res = await api.search(trimmed, 10)
        setResult(res)
        lastQuery.current = trimmed
        mark('search', 'works')
      } catch (e) {
        setResult(null)
        handleFail('search')(e)
      } finally {
        setSearching(false)
      }
    },
    [handleFail, mark],
  )

  const afterWrite = useCallback(
    async (msg: string) => {
      setNote(msg)
      setError(null)
      await loadStats()
      if (lastQuery.current) void runSearch(lastQuery.current)
    },
    [loadStats, runSearch],
  )

  async function doRefresh() {
    setRefreshing(true)
    setError(null)
    try {
      const { refreshed } = await api.refresh()
      mark('refresh', 'works')
      await afterWrite(
        refreshed > 0
          ? `Wrote ${refreshed} document${refreshed === 1 ? '' : 's'} to disk. They are searchable now.`
          : 'Nothing was waiting in memory.',
      )
    } catch (e) {
      handleFail('refresh')(e)
    } finally {
      setRefreshing(false)
    }
  }

  async function doMerge() {
    try {
      const { merged_segments } = await api.forceMerge()
      mark('merge', 'works')
      await afterWrite(`Merged ${merged_segments} segment file(s) into one.`)
    } catch (e) {
      handleFail('merge')(e)
    }
  }

  async function onDelete(id: string) {
    try {
      await api.deleteDocument(id)
      mark('delete', 'works')
      setDocs(forget(id))
      setOpenDoc(null)
      await afterWrite(`Deleted “${id}”.`)
    } catch (e) {
      handleFail('delete')(e)
    }
  }

  // The drawer is a fixed 28rem panel. Padding the *column* to clear it ate
  // 28rem out of `max-w-3xl` and squeezed the content down to ~300px — while
  // every `sm:` rule kept firing, because those read the viewport, not this box.
  // The pipeline row then overflowed its own card. Reserve the space on an outer
  // wrapper instead, and let the centred column shrink into what is left over.
  return (
    <div className={'min-h-screen transition-[padding] ' + (openDoc ? 'lg:pr-[28rem]' : '')}>
      <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <header className="mb-8 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold tracking-tight">Full-Text Search</h1>
            <p className="text-muted-foreground mt-1 text-sm">
              Add documents, make them searchable, search them. Follow the three steps.
            </p>
          </div>
          <span className="text-muted-foreground flex shrink-0 items-center gap-2 pt-1.5 text-xs">
            <span
              className={
                'size-2 rounded-full ' +
                (online === null ? 'bg-muted-foreground' : online ? 'bg-success' : 'bg-destructive')
              }
            />
            {online === null ? 'connecting' : online ? 'engine online' : 'engine offline'}
          </span>
        </header>

        {online === false && (
          <div className="border-destructive/40 bg-destructive/10 text-destructive mb-6 rounded-lg border px-4 py-3 text-sm">
            Can&apos;t reach the engine. Start it with{' '}
            <code className="font-mono">make run</code> in the project folder.
          </div>
        )}

        {error && (
          <div className="border-destructive/40 bg-destructive/10 text-destructive mb-6 rounded-lg border px-4 py-3 text-sm">
            {error}
          </div>
        )}

        {note && (
          <div className="border-success/40 bg-success/10 text-success mb-6 rounded-lg border px-4 py-3 text-sm">
            {note}
          </div>
        )}

        <div className="mb-6">
          <Pipeline stats={stats} />
        </div>

        <div className="flex flex-col gap-4">
          <Stage n={1} id="add" title="Add documents" status={status.add}>
            <AddDocuments
              onDone={(msg, added, shard) => {
                mark('add', 'works')
                setDocs(remember(added, shard))
                void afterWrite(msg)
              }}
              onFail={handleFail('add')}
            />
            <DocumentList
              docs={docs}
              selectedId={openDoc?.id ?? null}
              onSelect={setOpenDoc}
              onClear={() => {
                setDocs(forgetAll())
                setOpenDoc(null)
              }}
            />
          </Stage>

          <Stage n={2} id="refresh" title="Make them searchable" status={status.refresh}>
            <Button onClick={doRefresh} disabled={refreshing} className="w-full @sm:w-auto">
              <RefreshCw className={`size-4 ${refreshing ? 'animate-spin' : ''}`} />
              {refreshing ? 'Writing to disk…' : 'Refresh'}
            </Button>
          </Stage>

          <Stage n={3} id="search" title="Search" status={status.search}>
            <form
              onSubmit={(e) => {
                e.preventDefault()
                void runSearch(query)
              }}
              className="flex flex-col gap-2 @sm:flex-row"
            >
              <div className="relative flex-1">
                <SearchIcon className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
                <Input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Try: inverted index"
                  className="h-10 pl-9"
                />
              </div>
              <Button type="submit" disabled={searching || !query.trim()} className="h-10 @sm:w-28">
                {searching ? 'Searching…' : 'Search'}
              </Button>
            </form>

            <Results
              query={lastQuery.current}
              result={result}
              onDelete={onDelete}
              onOpen={setOpenDoc}
            />
          </Stage>
        </div>

        {/* Everything that is real but not part of the main loop, folded away so it
            cannot compete with the three steps for attention. */}
        <div className="mt-6">
          <button
            onClick={() => setShowAdvanced((v) => !v)}
            className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-xs transition-colors"
          >
            <ChevronDown className={`size-3.5 transition-transform ${showAdvanced ? '' : '-rotate-90'}`} />
            More: per-shard detail and segment merging
          </button>

          {showAdvanced && (
            <div className="bg-card mt-3 rounded-xl border p-5">
              <p className="text-muted-foreground mb-4 text-xs leading-relaxed">
                Your corpus is split across {stats?.shard_count ?? '?'} shards. Each holds its own
                index; a search asks all of them and merges the answers. Each refresh writes a new
                segment file, so segment counts climb over time — merging squashes them back down.
              </p>

              <div className="mb-4 overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="text-muted-foreground">
                    <tr className="border-b">
                      <th className="py-2 pr-4 font-medium">Shard</th>
                      <th className="py-2 pr-4 font-medium">Searchable docs</th>
                      <th className="py-2 pr-4 font-medium">Segment files</th>
                      <th className="py-2 pr-4 font-medium">Waiting</th>
                      <th className="py-2 font-medium">Deleted</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono tabular-nums">
                    {(stats?.shards ?? []).map((s) => (
                      <tr key={s.shard} className="border-b last:border-0">
                        <td className="py-2 pr-4">{s.shard}</td>
                        <td className="py-2 pr-4">{s.doc_count}</td>
                        <td className="py-2 pr-4">{s.segments}</td>
                        <td className="py-2 pr-4">{s.buffered}</td>
                        <td className="py-2">{s.deleted}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <Button variant="outline" size="sm" onClick={doMerge}>
                <Layers className="size-4" /> Merge segment files
              </Button>
              {status.merge === 'notBuilt' && (
                <p className="text-warning mt-2 text-xs">
                  Not built yet — src/full_text_search/merge.py (V4).
                </p>
              )}
            </div>
          )}
        </div>

        <DocumentDrawer doc={openDoc} onClose={() => setOpenDoc(null)} onDelete={onDelete} />

        <footer className="text-muted-foreground mt-10 text-center text-xs">
          project 20 · a search engine built from the inverted index up
        </footer>
      </div>
    </div>
  )
}
