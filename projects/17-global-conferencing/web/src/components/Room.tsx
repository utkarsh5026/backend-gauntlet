import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Globe, Mic, MicOff, Radio, Video, VideoOff } from 'lucide-react'
import { fetchTopology, publish, type GlobalTopology } from '@/api'
import { connectMedia, openCamera, proposeLayers, randomUfrag } from '@/webrtc'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

const EMPTY: GlobalTopology = { region: '—', rooms: [], relay_legs: [] }

export function Room({ room, name }: { room: string; name: string }) {
  const localRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const [topo, setTopo] = useState<GlobalTopology>(EMPTY)
  const [published, setPublished] = useState<Record<string, unknown> | null>(null)
  const [camOn, setCamOn] = useState(false)
  const [micOn, setMicOn] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const tick = () => fetchTopology().then(setTopo).catch(() => undefined)
    tick()
    const id = window.setInterval(tick, 2000)
    return () => {
      window.clearInterval(id)
      streamRef.current?.getTracks().forEach((t) => t.stop())
    }
  }, [])

  const goLive = async () => {
    setError(null)
    try {
      const stream = streamRef.current ?? (await openCamera())
      streamRef.current = stream
      if (localRef.current) localRef.current.srcObject = stream
      setCamOn(true)
      const res = await publish(room, proposeLayers(), randomUfrag())
      setPublished(res)
      try {
        await connectMedia(stream, res)
      } catch {
        // Media path is the reused-p15 TODO; signaling/placement already happened.
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const toggleCam = () => {
    const t = streamRef.current?.getVideoTracks()[0]
    if (t) {
      t.enabled = !t.enabled
      setCamOn(t.enabled)
    }
  }
  const toggleMic = () => {
    const t = streamRef.current?.getAudioTracks()[0]
    if (t) {
      t.enabled = !t.enabled
      setMicOn(t.enabled)
    }
  }

  const thisRoom = topo.rooms.find((r) => r.room_id === room)

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Tile label={`${name} (you) · ${topo.region}`} active={camOn}>
            {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
            <video ref={localRef} autoPlay playsInline muted className="size-full object-cover" />
          </Tile>
          <Tile label="remote" active={false}>
            <div className="text-muted-foreground flex size-full flex-col items-center justify-center gap-1 text-xs">
              <span>no media</span>
              <span className="opacity-70">reuse the p15 subscribe path here</span>
            </div>
          </Tile>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={goLive}>
            <Radio className="size-4" /> Publish (places the room)
          </Button>
          <Button variant="outline" size="icon" onClick={toggleMic} disabled={!streamRef.current}>
            {micOn ? <Mic className="size-4" /> : <MicOff className="size-4" />}
          </Button>
          <Button variant="outline" size="icon" onClick={toggleCam} disabled={!streamRef.current}>
            {camOn ? <Video className="size-4" /> : <VideoOff className="size-4" />}
          </Button>
        </div>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
            <div className="text-muted-foreground mt-1 text-xs">
              (Placement runs through consensus — expect this until V1 is built.)
            </div>
          </div>
        )}

        {published && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Publish response</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="overflow-x-auto rounded-md bg-black/40 p-3 font-mono text-xs">
                {JSON.stringify(published, null, 2)}
              </pre>
            </CardContent>
          </Card>
        )}
      </div>

      <div className="space-y-4">
        <Card className="h-fit">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Globe className="size-4" /> Global topology
            </CardTitle>
            <p className="text-muted-foreground text-xs">
              this node reports region <code className="font-mono">{topo.region}</code>
            </p>
          </CardHeader>
          <CardContent className="space-y-3">
            {topo.rooms.length === 0 && (
              <p className="text-muted-foreground text-xs">No rooms placed yet.</p>
            )}
            {topo.rooms.map((r) => (
              <div key={r.room_id} className="space-y-1 border-b pb-2 last:border-0">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-sm">{r.room_id}</span>
                  <Badge variant="outline" className="text-[10px]">
                    epoch {r.epoch}
                  </Badge>
                </div>
                <div className="flex flex-wrap items-center gap-1 text-xs">
                  <span className="text-muted-foreground">home</span>
                  <Badge className="text-[10px]">{r.home_region}</Badge>
                  <span className="text-muted-foreground ml-1">active</span>
                  {r.active_regions.map((reg) => (
                    <Badge
                      key={reg}
                      variant={reg === r.home_region ? 'default' : 'secondary'}
                      className="text-[10px]"
                    >
                      {reg}
                    </Badge>
                  ))}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card className="h-fit">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Cascade legs</CardTitle>
          </CardHeader>
          <CardContent>
            {topo.relay_legs.length === 0 ? (
              <p className="text-muted-foreground text-xs">No inter-SFU relays.</p>
            ) : (
              <ul className="space-y-1 font-mono text-xs">
                {topo.relay_legs.map((l, i) => (
                  <li key={i} className="flex justify-between">
                    <span>→ {l.region}</span>
                    <span className="text-muted-foreground">{l.tracks} tracks</span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        {thisRoom && thisRoom.home_region !== topo.region && (
          <p className="text-muted-foreground px-1 text-xs">
            This room is anchored in <b>{thisRoom.home_region}</b>; your region subscribes over a
            cascade leg (no hairpin).
          </p>
        )}
      </div>
    </div>
  )
}

function Tile({ label, active, children }: { label: string; active: boolean; children: ReactNode }) {
  return (
    <div className="relative aspect-video overflow-hidden rounded-xl border bg-black">
      {children}
      <div className="absolute inset-x-0 bottom-0 flex items-center justify-between bg-gradient-to-t from-black/70 to-transparent px-2 py-1.5">
        <span className="text-xs text-white">{label}</span>
        {active && <span className="size-2 rounded-full bg-[var(--success)]" />}
      </div>
    </div>
  )
}
