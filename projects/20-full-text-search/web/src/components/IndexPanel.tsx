import { useState } from 'react'
import { Database, Layers, Plus, RefreshCw } from 'lucide-react'

import * as api from '@/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { SAMPLE_CORPUS } from '@/sample-corpus'

interface Props {
  /** Called after any mutation so the parent can refresh stats (and re-run the
   *  current search). `note` is a short human message about what happened. */
  onChanged: (note: string) => void
  onError: (msg: string) => void
}

/** The write side: index a single document, seed the sample corpus, and run the
 *  admin operations (refresh → make buffered docs searchable; force-merge →
 *  compact segments + drop tombstones). Every write is followed by a refresh so
 *  new docs show up in search immediately. */
export function IndexPanel({ onChanged, onError }: Props) {
  const [id, setId] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState<string | null>(null)

  async function run<T>(tag: string, fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(tag)
    try {
      return await fn()
    } catch (e) {
      onError(e instanceof api.ApiError ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  async function addDocument() {
    if (!text.trim()) return
    await run('add', async () => {
      await api.indexDocument({ id: id.trim() || undefined, text: text.trim() })
      await api.refresh()
      setId('')
      setText('')
      onChanged('Indexed 1 document and refreshed.')
    })
  }

  async function seed() {
    await run('seed', async () => {
      const { indexed } = await api.bulk(SAMPLE_CORPUS)
      await api.refresh()
      onChanged(`Seeded ${indexed} sample documents and refreshed.`)
    })
  }

  async function doRefresh() {
    await run('refresh', async () => {
      const { refreshed } = await api.refresh()
      onChanged(`Refreshed ${refreshed} buffered document(s) into segments.`)
    })
  }

  async function doMerge() {
    await run('merge', async () => {
      const { merged_segments } = await api.forceMerge()
      onChanged(`Force-merged ${merged_segments} segment(s).`)
    })
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Plus className="size-4" /> Index a document
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="doc-id">
              External id <span className="text-muted-foreground font-normal">(optional)</span>
            </Label>
            <Input
              id="doc-id"
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="e.g. article-42"
              className="font-mono"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="doc-text">Text</Label>
            <Textarea
              id="doc-text"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Paste document text to analyze and index…"
              rows={4}
            />
          </div>
          <Button onClick={addDocument} disabled={busy !== null || !text.trim()}>
            {busy === 'add' ? 'Indexing…' : 'Index + refresh'}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="size-4" /> Corpus & admin
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <Button variant="secondary" onClick={seed} disabled={busy !== null}>
            {busy === 'seed' ? 'Seeding…' : `Seed sample corpus (${SAMPLE_CORPUS.length} docs)`}
          </Button>
          <div className="grid grid-cols-2 gap-2">
            <Button variant="outline" onClick={doRefresh} disabled={busy !== null}>
              <RefreshCw className={busy === 'refresh' ? 'animate-spin' : ''} /> Refresh
            </Button>
            <Button variant="outline" onClick={doMerge} disabled={busy !== null}>
              <Layers /> Force-merge
            </Button>
          </div>
          <p className="text-muted-foreground text-xs leading-relaxed">
            <span className="text-foreground font-medium">Refresh</span> flushes buffered docs into a
            searchable segment (V2). <span className="text-foreground font-medium">Force-merge</span>{' '}
            compacts segments and drops tombstoned docs (V4).
          </p>
          <Badge variant="outline" className="text-muted-foreground mt-1 font-normal">
            write routes need an API key once the security horizontal lands
          </Badge>
        </CardContent>
      </Card>
    </div>
  )
}
