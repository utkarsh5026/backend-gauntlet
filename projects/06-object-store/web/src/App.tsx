import { useCallback, useEffect, useState } from 'react'
import { Boxes } from 'lucide-react'

import { health } from './api'
import { cn } from '@/lib/utils'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import BucketBar from './components/BucketBar'
import Objects from './components/Objects'
import MultipartPanel from './components/Multipart'

const BUCKETS_KEY = 'os.buckets'
const CURRENT_KEY = 'os.currentBucket'

function loadBuckets(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(BUCKETS_KEY) || '[]')
    return Array.isArray(v) ? v : []
  } catch {
    return []
  }
}

export default function App() {
  const [buckets, setBuckets] = useState<string[]>(loadBuckets)
  const [bucket, setBucket] = useState<string>(() => localStorage.getItem(CURRENT_KEY) || '')
  const [online, setOnline] = useState<boolean | null>(null)

  useEffect(() => {
    localStorage.setItem(BUCKETS_KEY, JSON.stringify(buckets))
  }, [buckets])
  useEffect(() => {
    localStorage.setItem(CURRENT_KEY, bucket)
  }, [bucket])

  const rememberBucket = useCallback((b: string) => {
    setBuckets((prev) => (prev.includes(b) ? prev : [...prev, b].sort()))
    setBucket(b)
  }, [])

  const forgetBucket = useCallback((b: string) => {
    setBuckets((prev) => prev.filter((x) => x !== b))
    setBucket((cur) => (cur === b ? '' : cur))
  }, [])

  useEffect(() => {
    let alive = true
    const ping = async () => {
      const ok = await health()
      if (alive) setOnline(ok)
    }
    ping()
    const id = setInterval(ping, 5000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  return (
    <div className="mx-auto max-w-5xl px-6 py-8 pb-16">
      <header className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="bg-secondary text-foreground flex size-10 items-center justify-center rounded-lg border">
            <Boxes className="size-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Object Store Console</h1>
            <p className="text-muted-foreground text-xs">S3-compatible · backend-gauntlet project 06</p>
          </div>
        </div>
        <div className="text-muted-foreground flex items-center gap-2 text-xs">
          <span
            className={cn(
              'size-2 rounded-full',
              online === null && 'bg-muted-foreground/50',
              online === true && 'bg-success shadow-[0_0_8px_var(--success)]',
              online === false && 'bg-destructive shadow-[0_0_8px_var(--destructive)]',
            )}
          />
          {online === null ? 'checking…' : online ? 'backend up' : 'backend down'}
          <code className="text-muted-foreground/70 font-mono">/s3</code>
        </div>
      </header>

      <div className="border-border bg-muted/30 text-muted-foreground mt-6 rounded-lg border px-4 py-2.5 text-sm">
        The backend ships with <code className="font-mono text-foreground">todo!()</code> bodies — endpoints
        error until you implement V1–V4 in <code className="font-mono text-foreground">src/</code>. This console
        is the client you build against.
      </div>

      <div className="mt-6">
        <BucketBar
          buckets={buckets}
          bucket={bucket}
          onSelect={setBucket}
          onRemember={rememberBucket}
          onForget={forgetBucket}
        />
      </div>

      {bucket ? (
        <Tabs defaultValue="objects" className="mt-8">
          <TabsList>
            <TabsTrigger value="objects">Objects</TabsTrigger>
            <TabsTrigger value="multipart">Multipart upload</TabsTrigger>
          </TabsList>
          <TabsContent value="objects" className="mt-4">
            <Objects bucket={bucket} />
          </TabsContent>
          <TabsContent value="multipart" className="mt-4">
            <MultipartPanel bucket={bucket} />
          </TabsContent>
        </Tabs>
      ) : (
        <div className="text-muted-foreground mt-16 text-center text-sm">
          Create or select a bucket to begin.
        </div>
      )}

      <footer className="text-muted-foreground/70 mt-10 text-center font-mono text-xs">
        proxy <code className="font-mono">/s3</code> → object store (default{' '}
        <code className="font-mono">localhost:9006</code>) · set{' '}
        <code className="font-mono">OBJECT_STORE_URL</code> to change
      </footer>
    </div>
  )
}
