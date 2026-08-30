import { useState } from 'react'
import { Plus, Sparkles } from 'lucide-react'

import * as api from '@/api'
import type { NewDocument } from '@/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { SAMPLE_CORPUS } from '@/sample-corpus'

interface Props {
  /** Reports what was added so the parent can record it for the document list —
   *  the engine cannot list documents back to us, so this is the only moment we
   *  ever see their text. `shard` is known only for single adds; `_bulk` returns
   *  a count and nothing else. */
  onDone: (note: string, docs: NewDocument[], shard: number | null) => void
  onFail: (e: unknown) => void
}

/** Step 1: put text into the engine.
 *
 *  Deliberately does NOT refresh afterwards, which the old panel did behind a
 *  single "Index + refresh" button. Auto-refreshing hides the exact thing this
 *  console is trying to make obvious: a document you just added is not findable
 *  until you flush it to disk. Leaving the two apart makes the "waiting in
 *  memory" counter climb and sit there until you press step 2 yourself.
 */
export function AddDocuments({ onDone, onFail }: Props) {
  const [text, setText] = useState('')
  const [id, setId] = useState('')
  const [busy, setBusy] = useState<'seed' | 'one' | null>(null)

  async function seed() {
    setBusy('seed')
    try {
      const { indexed } = await api.bulk(SAMPLE_CORPUS)
      onDone(`Added ${indexed} example documents. They are waiting in memory.`, SAMPLE_CORPUS, null)
    } catch (e) {
      onFail(e)
    } finally {
      setBusy(null)
    }
  }

  async function addOne() {
    if (!text.trim()) return
    setBusy('one')
    try {
      const doc: NewDocument = { id: id.trim() || undefined, text: text.trim() }
      const { shard } = await api.indexDocument(doc)
      setText('')
      setId('')
      onDone('Added 1 document. It is waiting in memory.', [doc], shard)
    } catch (e) {
      onFail(e)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Button variant="secondary" onClick={seed} disabled={busy !== null} className="justify-start">
        <Sparkles className="size-4" />
        {busy === 'seed' ? 'Adding…' : `Load ${SAMPLE_CORPUS.length} example documents`}
      </Button>

      <div className="flex items-center gap-3">
        <span className="bg-border h-px flex-1" />
        <span className="text-muted-foreground text-xs">or write your own</span>
        <span className="bg-border h-px flex-1" />
      </div>

      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Type or paste any text — a paragraph, a note, an article…"
        rows={3}
      />
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          value={id}
          onChange={(e) => setId(e.target.value)}
          placeholder="Name it (optional) — e.g. my-note"
          className="font-mono text-sm"
        />
        <Button onClick={addOne} disabled={busy !== null || !text.trim()} className="sm:w-32">
          <Plus className="size-4" />
          {busy === 'one' ? 'Adding…' : 'Add'}
        </Button>
      </div>

      <p className="text-muted-foreground text-xs leading-relaxed">
        Nothing is searchable yet — that is step 2. This is what &ldquo;near&#8209;real&#8209;time
        search&rdquo; means: documents become findable on a refresh, not the instant you add them.
      </p>
    </div>
  )
}
