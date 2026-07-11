import { useState } from 'react'
import { Plus, X } from 'lucide-react'

import * as api from '../api'
import { errMsg } from '../util'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent } from '@/components/ui/card'

export default function BucketBar({
  buckets,
  bucket,
  onSelect,
  onRemember,
  onForget,
}: {
  buckets: string[]
  bucket: string
  onSelect: (b: string) => void
  onRemember: (b: string) => void
  onForget: (b: string) => void
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const create = async () => {
    const b = name.trim()
    if (!b) return
    setBusy(true)
    setErr(null)
    try {
      await api.createBucket(b)
      onRemember(b)
      setName('')
    } catch (e) {
      // If it already exists on the server, just adopt & select it.
      if (e instanceof api.ApiError && e.status === 409) {
        onRemember(b)
        setName('')
      } else {
        setErr(errMsg(e))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="py-4">
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-muted-foreground w-14 text-xs tracking-wide uppercase">bucket</span>
          <Input
            className="w-56 font-mono"
            placeholder="new-bucket-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && create()}
          />
          <Button onClick={create} disabled={busy}>
            <Plus />
            {busy ? 'creating…' : 'Create / use'}
          </Button>
          <span className="text-muted-foreground text-xs">
            3–63 chars · a–z 0–9 · hyphens (no leading/trailing)
          </span>
        </div>

        {err && (
          <p className="text-destructive bg-destructive/10 border-destructive/30 rounded-md border px-3 py-2 font-mono text-xs">
            {err}
          </p>
        )}

        {buckets.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {buckets.map((b) => (
              <span
                key={b}
                className={cn(
                  'bg-secondary inline-flex items-center overflow-hidden rounded-full border',
                  b === bucket && 'border-primary/60 bg-primary/10',
                )}
              >
                <button
                  className={cn(
                    'cursor-pointer py-1 pr-1 pl-3 font-mono text-sm',
                    b === bucket ? 'text-foreground font-medium' : 'text-muted-foreground',
                  )}
                  onClick={() => onSelect(b)}
                >
                  {b}
                </button>
                <button
                  className="text-muted-foreground hover:text-destructive cursor-pointer py-1 pr-2.5 pl-1"
                  title="forget locally (does not delete the bucket)"
                  onClick={() => onForget(b)}
                >
                  <X className="size-3.5" />
                </button>
              </span>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
