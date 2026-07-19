import { useEffect, useState } from 'react'
import { Radio, RefreshCw } from 'lucide-react'
import { fetchLiveKeys, mediaPlaylistUrl } from '@/api'
import { LivePlayer } from '@/components/LivePlayer'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export default function App() {
  const [keys, setKeys] = useState<string[]>([])
  const [key, setKey] = useState('testkey')
  const [src, setSrc] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const refresh = () => {
    fetchLiveKeys()
      .then((k) => {
        setKeys(k)
        setErr(null)
      })
      .catch((e) => setErr(String(e)))
  }

  useEffect(() => {
    refresh()
    const id = window.setInterval(refresh, 5000)
    return () => window.clearInterval(id)
  }, [])

  const watch = (k: string) => {
    setKey(k)
    setSrc(mediaPlaylistUrl(k))
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-6">
      <header className="flex items-center gap-3">
        <Radio className="text-primary size-6" />
        <div>
          <h1 className="text-xl font-semibold">Live Ingest</h1>
          <p className="text-muted-foreground text-sm">
            Project 13 · RTMP → LL-HLS — the latency readout is the scoreboard
          </p>
        </div>
      </header>

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 pt-6">
          <div className="grid gap-1.5">
            <Label htmlFor="key">Stream key</Label>
            <Input
              id="key"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && watch(key)}
              className="w-56 font-mono"
              placeholder="testkey"
            />
          </div>
          <Button onClick={() => watch(key)}>Watch</Button>
          <div className="ml-auto flex items-center gap-2">
            <span className="text-muted-foreground text-xs">
              {keys.length > 0 ? `on air: ${keys.join(', ')}` : 'no streams on air'}
            </span>
            <Button size="icon" variant="ghost" onClick={refresh}>
              <RefreshCw className="size-3.5" />
            </Button>
          </div>
        </CardContent>
      </Card>

      {src ? (
        <LivePlayer src={src} />
      ) : (
        <div className="text-muted-foreground rounded-xl border border-dashed py-16 text-center text-sm">
          Push a stream, then Watch. Publish with:
          <br />
          <code className="mt-2 inline-block font-mono text-xs">
            ffmpeg -re -i in.mp4 -c copy -f flv rtmp://localhost:1935/live/{key}
          </code>
        </div>
      )}

      {err && (
        <p className="text-muted-foreground text-center text-xs">
          Live list unavailable ({err}). Is the backend up on :8080?
        </p>
      )}
    </div>
  )
}
