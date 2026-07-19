import Hls from 'hls.js'
import { useEffect, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

type Live = {
  latencySec: number | null
  targetSec: number | null
  bufferSec: number
  atEdge: boolean
  stalls: number
}

const EMPTY: Live = { latencySec: null, targetSec: null, bufferSec: 0, atEdge: false, stalls: 0 }

/**
 * A low-latency HLS player (`lowLatencyMode: true`). The whole point of project
 * 13 is shaving the ~15–30s of a normal HLS window down to sub-second by serving
 * 200ms parts and blocking playlist reloads — so this player surfaces the live
 * latency prominently. If your parts and PART-HOLD-BACK are right, the number
 * sits well under a second and the LIVE badge stays lit.
 */
export function LivePlayer({ src }: { src: string }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  const stallsRef = useRef(0)
  const [live, setLive] = useState<Live>(EMPTY)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const video = videoRef.current
    if (!video || !src) return
    setError(null)
    stallsRef.current = 0
    setLive(EMPTY)

    if (!Hls.isSupported()) {
      // Safari drives LL-HLS natively (blocking reload + parts) with no JS.
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src
        return
      }
      setError('This browser has neither MSE nor native LL-HLS support.')
      return
    }

    const hls = new Hls({ lowLatencyMode: true, enableWorker: true, backBufferLength: 4 })
    hlsRef.current = hls
    hls.loadSource(src)
    hls.attachMedia(video)
    hls.on(Hls.Events.ERROR, (_evt, data) => {
      if (data.details === 'bufferStalledError') stallsRef.current += 1
      if (data.fatal) setError(`${data.type} — ${data.details}`)
    })

    return () => {
      hls.destroy()
      hlsRef.current = null
    }
  }, [src])

  useEffect(() => {
    const id = window.setInterval(() => {
      const video = videoRef.current
      const hls = hlsRef.current
      if (!video || !hls) return
      const bufEnd = video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0
      const latency = hls.latency
      setLive({
        latencySec: Number.isFinite(latency) ? latency : null,
        targetSec: hls.targetLatency ?? null,
        bufferSec: Math.max(0, bufEnd - video.currentTime),
        atEdge: bufEnd - video.currentTime < 1.5 && !video.paused,
        stalls: stallsRef.current,
      })
    }, 250)
    return () => window.clearInterval(id)
  }, [])

  const jumpToEdge = () => {
    const video = videoRef.current
    const hls = hlsRef.current
    if (!video || !hls) return
    const edge = hls.liveSyncPosition
    if (edge != null) video.currentTime = edge
    void video.play()
  }

  const latencyMs = live.latencySec == null ? null : Math.round(live.latencySec * 1000)
  const latencyTone =
    latencyMs == null ? 'secondary' : latencyMs < 1500 ? 'default' : 'destructive'

  return (
    <div className="space-y-3">
      <div className="relative overflow-hidden rounded-xl border bg-black">
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <video ref={videoRef} controls playsInline autoPlay muted className="aspect-video w-full" />
        <div className="absolute left-3 top-3">
          <Badge variant={live.atEdge ? 'destructive' : 'secondary'} className="gap-1.5">
            <span className="inline-block size-2 rounded-full bg-current" />
            {live.atEdge ? 'LIVE' : 'BEHIND'}
          </Badge>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="live latency" value={latencyMs == null ? '—' : `${latencyMs} ms`} tone={latencyTone} />
        <Stat
          label="target"
          value={live.targetSec == null ? '—' : `${Math.round(live.targetSec * 1000)} ms`}
        />
        <Stat label="buffer" value={`${live.bufferSec.toFixed(2)} s`} />
        <Stat label="stalls" value={String(live.stalls)} tone={live.stalls > 0 ? 'destructive' : undefined} />
      </div>

      <Button size="sm" variant="outline" onClick={jumpToEdge}>
        Jump to live edge
      </Button>
    </div>
  )
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'default' | 'secondary' | 'destructive'
}) {
  const color =
    tone === 'destructive'
      ? 'text-destructive'
      : tone === 'default'
        ? 'text-[var(--success)]'
        : 'text-foreground'
  return (
    <div className="rounded-lg border p-3">
      <div className="text-muted-foreground text-[11px] uppercase tracking-wide">{label}</div>
      <div className={`font-mono text-lg ${color}`}>{value}</div>
    </div>
  )
}
