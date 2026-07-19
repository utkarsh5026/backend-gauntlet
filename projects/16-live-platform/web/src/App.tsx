import { useEffect, useState } from 'react'
import { Tv, Users } from 'lucide-react'
import { fetchStatus, masterPlaylistUrl, type PlatformStatus } from '@/api'
import { ChatPanel } from '@/components/ChatPanel'
import { PlatformPlayer } from '@/components/PlatformPlayer'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export default function App() {
  const [stream, setStream] = useState('demo')
  const [user, setUser] = useState('guest')
  const [watching, setWatching] = useState<string | null>(null)
  const [status, setStatus] = useState<PlatformStatus | null>(null)

  useEffect(() => {
    const tick = () => fetchStatus().then(setStatus).catch(() => setStatus(null))
    tick()
    const id = window.setInterval(tick, 5000)
    return () => window.clearInterval(id)
  }, [])

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <header className="flex flex-wrap items-center gap-3">
        <Tv className="text-primary size-6" />
        <div className="mr-auto">
          <h1 className="text-xl font-semibold">Live Platform</h1>
          <p className="text-muted-foreground text-sm">
            Project 16 · Twitch-lite watch page — LL-HLS playback + channel chat
          </p>
        </div>
        {status && (
          <div className="flex gap-2">
            <Badge variant="secondary">{status.streams_live} live</Badge>
            <Badge variant="secondary" className="gap-1">
              <Users className="size-3" /> {status.chat.active_channels} channels
            </Badge>
            <Badge variant="secondary">queue {status.transcode.queue_depth}</Badge>
          </div>
        )}
      </header>

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 pt-6">
          <div className="grid gap-1.5">
            <Label htmlFor="stream">Channel</Label>
            <Input
              id="stream"
              value={stream}
              onChange={(e) => setStream(e.target.value)}
              className="w-44 font-mono"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="user">Your name</Label>
            <Input id="user" value={user} onChange={(e) => setUser(e.target.value)} className="w-44" />
          </div>
          <Button onClick={() => setWatching(stream)}>Watch channel</Button>
        </CardContent>
      </Card>

      {watching ? (
        <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
          <PlatformPlayer src={masterPlaylistUrl(watching)} />
          <div className="h-[28rem] lg:h-auto">
            <ChatPanel stream={watching} user={user} />
          </div>
        </div>
      ) : (
        <div className="text-muted-foreground rounded-xl border border-dashed py-16 text-center text-sm">
          Enter a channel and hit Watch. Start one with an ingest webhook:
          <br />
          <code className="mt-2 inline-block font-mono text-xs">
            curl -XPOST localhost:8080/ingest/start -d {'{"stream":"demo"}'}
          </code>
        </div>
      )}
    </div>
  )
}
