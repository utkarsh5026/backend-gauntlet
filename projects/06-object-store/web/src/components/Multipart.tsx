import { useEffect, useMemo, useState } from 'react'
import { Download, Layers } from 'lucide-react'

import * as api from '../api'
import type { CompletedPart } from '../api'
import { fmtBytes, errMsg } from '../util'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

type Phase = 'idle' | 'initiating' | 'uploading' | 'completing' | 'done' | 'error'

interface Chunk {
  partNumber: number
  start: number
  end: number
  size: number
}

interface PartProgress {
  status: 'pending' | 'uploading' | 'done' | 'error'
  pct: number
  etag?: string
}

const PART_SIZES = [1, 5, 8, 16] // MiB
const CONCURRENCY = 4

function computeChunks(file: File | null, partBytes: number): Chunk[] {
  if (!file) return []
  const count = Math.max(1, Math.ceil(file.size / partBytes))
  return Array.from({ length: count }, (_, i) => {
    const start = i * partBytes
    const end = Math.min(file.size, start + partBytes)
    return { partNumber: i + 1, start, end, size: end - start }
  })
}

/** Run `worker` over `items` with at most `limit` in flight. */
async function pool<T>(items: T[], limit: number, worker: (item: T) => Promise<void>) {
  let idx = 0
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (idx < items.length) {
      const cur = idx++
      await worker(items[cur])
    }
  })
  await Promise.all(runners)
}

const STATUS_TINT: Record<PartProgress['status'], string> = {
  pending: 'border-border',
  uploading: 'border-primary/50',
  done: 'border-success/50',
  error: 'border-destructive/60',
}

export default function MultipartPanel({ bucket }: { bucket: string }) {
  const [file, setFile] = useState<File | null>(null)
  const [key, setKey] = useState('')
  const [partSizeMiB, setPartSizeMiB] = useState(5)
  const [phase, setPhase] = useState<Phase>('idle')
  const [uploadId, setUploadId] = useState<string | null>(null)
  const [progress, setProgress] = useState<Record<number, PartProgress>>({})
  const [resultEtag, setResultEtag] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [log, setLog] = useState<string[]>([])

  const chunks = useMemo(() => computeChunks(file, partSizeMiB * 1024 * 1024), [file, partSizeMiB])
  const effectiveKey = key.trim() || file?.name || ''

  // Reset transient state whenever the file or the part size changes.
  useEffect(() => {
    setProgress({})
    setPhase('idle')
    setUploadId(null)
    setResultEtag(null)
    setError(null)
    setLog([])
  }, [file, partSizeMiB])

  const say = (line: string) => setLog((prev) => [...prev, line])
  const setPart = (n: number, patch: Partial<PartProgress>) =>
    setProgress((prev) => {
      const base: PartProgress = prev[n] ?? { status: 'pending', pct: 0 }
      return { ...prev, [n]: { ...base, ...patch } }
    })

  const running = phase === 'initiating' || phase === 'uploading' || phase === 'completing'

  const start = async () => {
    if (!file || !effectiveKey) return
    setPhase('initiating')
    setError(null)
    setResultEtag(null)
    setProgress({})
    setLog([])
    say(`initiate — POST /${bucket}/${effectiveKey}?uploads`)
    try {
      const init = await api.initiateMultipart(bucket, effectiveKey, file.type || 'application/octet-stream')
      setUploadId(init.uploadId)
      say(`  uploadId = ${init.uploadId}`)
      say(`uploading ${chunks.length} part(s), ${CONCURRENCY} in flight…`)
      setPhase('uploading')

      const completed: CompletedPart[] = []
      await pool(chunks, CONCURRENCY, async (c) => {
        setPart(c.partNumber, { status: 'uploading', pct: 0 })
        const blob = file.slice(c.start, c.end)
        const r = await api.uploadPart(bucket, effectiveKey, init.uploadId, c.partNumber, blob, (l, t) =>
          setPart(c.partNumber, { pct: Math.round((l / t) * 100) }),
        )
        const etag = r.etag ?? ''
        setPart(c.partNumber, { status: 'done', pct: 100, etag })
        completed.push({ partNumber: c.partNumber, etag })
        say(`  part ${c.partNumber} ✓  md5=${etag}`)
      })

      completed.sort((a, b) => a.partNumber - b.partNumber)
      setPhase('completing')
      say(`complete — POST /${bucket}/${effectiveKey}?uploadId=…  (${completed.length} parts)`)
      const res = await api.completeMultipart(bucket, effectiveKey, init.uploadId, completed)
      const etag = res.etag ?? '(no etag returned)'
      setResultEtag(etag)
      setPhase('done')
      say(`  done ✓  object ETag = ${etag}`)
    } catch (e) {
      setError(errMsg(e))
      setPhase('error')
      say(`  ✗ ${errMsg(e)}`)
    }
  }

  const abort = async () => {
    if (!uploadId) return
    try {
      await api.abortMultipart(bucket, effectiveKey, uploadId)
      say('aborted — staged parts discarded')
      setPhase('idle')
      setUploadId(null)
    } catch (e) {
      setError(errMsg(e))
    }
  }

  return (
    <Card>
      <CardContent className="space-y-4">
        <div className="border-border bg-muted/30 text-muted-foreground border-l-primary flex gap-3 rounded-md border border-l-2 px-4 py-3 text-sm">
          <Layers className="mt-0.5 size-4 shrink-0" />
          <p>
            Splits the file in the browser, uploads each part as its own{' '}
            <code className="text-foreground font-mono">PUT …?partNumber=N</code> (parallel, retryable), then{' '}
            <code className="text-foreground font-mono">POST …?uploadId</code> to assemble. The final object
            ETag ends in <code className="text-foreground font-mono">-N</code> — the multipart signature.
          </p>
        </div>

        {/* form */}
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-muted-foreground w-20 text-xs tracking-wide uppercase">file</span>
            <Input
              type="file"
              className="max-w-xs"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-muted-foreground w-20 text-xs tracking-wide uppercase">key</span>
            <Input
              className="min-w-40 flex-1 font-mono"
              placeholder={file?.name || 'object-key'}
              value={key}
              onChange={(e) => setKey(e.target.value)}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-muted-foreground w-20 text-xs tracking-wide uppercase">part size</span>
            <Select value={String(partSizeMiB)} onValueChange={(v) => setPartSizeMiB(Number(v))}>
              <SelectTrigger className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PART_SIZES.map((s) => (
                  <SelectItem key={s} value={String(s)}>
                    {s} MiB
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {file && (
              <span className="text-muted-foreground text-xs">
                {fmtBytes(file.size)} → {chunks.length} part{chunks.length === 1 ? '' : 's'}
              </span>
            )}
          </div>
          <div className="flex gap-2 pt-1">
            <Button onClick={start} disabled={!file || running}>
              {running ? `${phase}…` : 'Start multipart upload'}
            </Button>
            {uploadId && phase !== 'done' && (
              <Button variant="destructive" onClick={abort} disabled={phase === 'completing'}>
                Abort
              </Button>
            )}
          </div>
        </div>

        {error && (
          <p className="text-destructive bg-destructive/10 border-destructive/30 rounded-md border px-3 py-2 font-mono text-xs">
            {error}
          </p>
        )}

        {resultEtag && (
          <div className="border-success/40 bg-success/10 space-y-2 rounded-lg border p-4">
            <Badge className="bg-success text-white">assembled ✓</Badge>
            <div className="font-mono text-lg break-all">{resultEtag}</div>
            <a
              className="text-primary inline-flex items-center gap-1.5 text-sm hover:underline"
              href={api.objectUrl(bucket, effectiveKey)}
              target="_blank"
              rel="noreferrer"
            >
              <Download className="size-4" />
              download the assembled object
            </a>
          </div>
        )}

        {/* per-part grid */}
        {chunks.length > 0 && (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {chunks.map((c) => {
              const p = progress[c.partNumber] ?? { status: 'pending' as const, pct: 0 }
              return (
                <div
                  key={c.partNumber}
                  className={cn('bg-secondary/40 space-y-2 rounded-lg border p-3', STATUS_TINT[p.status])}
                >
                  <div className="flex items-center justify-between text-xs">
                    <span className="font-mono">part {c.partNumber}</span>
                    <span className="text-muted-foreground">{fmtBytes(c.size)}</span>
                  </div>
                  <Progress value={p.pct} />
                  <div className="text-muted-foreground truncate font-mono text-[11px]" title={p.etag}>
                    {p.status === 'done' ? p.etag : p.status}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {log.length > 0 && (
          <pre className="bg-background text-muted-foreground max-h-56 overflow-auto rounded-lg border p-3 font-mono text-xs whitespace-pre-wrap">
            {log.join('\n')}
          </pre>
        )}
      </CardContent>
    </Card>
  )
}
