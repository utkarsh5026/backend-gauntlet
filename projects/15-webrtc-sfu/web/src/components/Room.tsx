import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Mic, MicOff, Radio, Video, VideoOff } from 'lucide-react'
import { fetchTopology, publish, type PeerHandle, type Topology } from '@/api'
import { connectMedia, openCamera, proposeLayers, randomUfrag } from '@/webrtc'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

export function Room({ room, name }: { room: string; name: string }) {
  const localRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const [topo, setTopo] = useState<Topology>({ rooms: [] })
  const [handle, setHandle] = useState<PeerHandle | null>(null)
  const [mediaNote, setMediaNote] = useState<string | null>(null)
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
    setMediaNote(null)
    try {
      const stream = streamRef.current ?? (await openCamera())
      streamRef.current = stream
      if (localRef.current) localRef.current.srcObject = stream
      setCamOn(true)
      const h = await publish(room, proposeLayers(), randomUfrag())
      setHandle(h)
      // Signaling done; the media path is the owner's V1/V3 work.
      try {
        await connectMedia(stream, h)
      } catch (e) {
        setMediaNote(e instanceof Error ? e.message : String(e))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const toggleCam = () => {
    const track = streamRef.current?.getVideoTracks()[0]
    if (track) {
      track.enabled = !track.enabled
      setCamOn(track.enabled)
    }
  }
  const toggleMic = () => {
    const track = streamRef.current?.getAudioTracks()[0]
    if (track) {
      track.enabled = !track.enabled
      setMicOn(track.enabled)
    }
  }

  const myRoom = topo.rooms.find((r) => r.room === room)
  const remotes = (myRoom?.peers ?? []).filter((p) => p.id !== handle?.peer_id)

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_18rem]">
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Tile label={`${name} (you)`} active={camOn}>
            {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
            <video ref={localRef} autoPlay playsInline muted className="size-full object-cover" />
          </Tile>
          {remotes.map((p) => (
            <Tile key={p.id} label={`peer #${p.id} · ${p.role}`} active={false}>
              <div className="text-muted-foreground flex size-full flex-col items-center justify-center gap-1 text-xs">
                <span>no media</span>
                <span className="opacity-70">subscribe wiring is your TODO</span>
              </div>
            </Tile>
          ))}
          {remotes.length === 0 && (
            <Tile label="waiting for peers" active={false}>
              <div className="text-muted-foreground flex size-full items-center justify-center text-xs">
                open a second tab / point gstreamer here
              </div>
            </Tile>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={goLive}>
            <Radio className="size-4" /> Go live (publish)
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
          </div>
        )}

        {handle && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Signaling response</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-xs">
              <dl className="grid grid-cols-[7rem_1fr] gap-1 font-mono">
                <dt className="text-muted-foreground">peer_id</dt>
                <dd>{handle.peer_id}</dd>
                <dt className="text-muted-foreground">ice_ufrag</dt>
                <dd>{handle.ice_ufrag}</dd>
                <dt className="text-muted-foreground">media_addr</dt>
                <dd>{handle.media_addr}</dd>
                <dt className="text-muted-foreground">out_ssrc</dt>
                <dd>{handle.out_ssrc ?? '—'}</dd>
              </dl>
              {mediaNote && (
                <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-amber-500">
                  {mediaNote}
                </p>
              )}
            </CardContent>
          </Card>
        )}
      </div>

      <Card className="h-fit">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Topology · GET /rooms</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {topo.rooms.length === 0 && (
            <p className="text-muted-foreground text-xs">No rooms yet.</p>
          )}
          {topo.rooms.map((r) => (
            <div key={r.room} className="space-y-1">
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm">{r.room}</span>
                <Badge variant="secondary" className="text-[10px]">
                  {r.peers.length} peers
                </Badge>
              </div>
              <ul className="text-muted-foreground space-y-0.5 pl-3 font-mono text-xs">
                {r.peers.map((p) => (
                  <li key={p.id}>
                    #{p.id} · {p.role}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}

function Tile({
  label,
  active,
  children,
}: {
  label: string
  active: boolean
  children: ReactNode
}) {
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
