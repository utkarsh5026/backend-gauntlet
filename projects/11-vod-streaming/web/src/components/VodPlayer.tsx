import Hls, { type Level } from 'hls.js'
import { useEffect, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type SwitchEntry = { at: number; label: string }

type Stats = {
  currentLevel: number
  bufferSec: number
  droppedFrames: number
  bitrateKbps: number
}

const EMPTY_STATS: Stats = { currentLevel: -1, bufferSec: 0, droppedFrames: 0, bitrateKbps: 0 }

function levelLabel(l: Level): string {
  return `${l.height}p · ${Math.round(l.bitrate / 1000)} kbps`
}

/**
 * A working hls.js player. curl can prove a segment is 200-OK, but only a real
 * player proves adaptive bitrate: watch the level list populate from the master
 * playlist, then force a rung and see the switch land in the log.
 */
export function VodPlayer({ src }: { src: string }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  const [levels, setLevels] = useState<Level[]>([])
  const [stats, setStats] = useState<Stats>(EMPTY_STATS)
  const [switches, setSwitches] = useState<SwitchEntry[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const video = videoRef.current
    if (!video || !src) return
    setError(null)
    setSwitches([])
    setLevels([])
    setStats(EMPTY_STATS)

    // Safari & iOS play HLS natively — no Media Source Extensions needed there.
    if (!Hls.isSupported()) {
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src
        return
      }
      setError('This browser has neither MSE nor native HLS support.')
      return
    }

    const hls = new Hls({ enableWorker: true })
    hlsRef.current = hls
    hls.loadSource(src)
    hls.attachMedia(video)

    hls.on(Hls.Events.MANIFEST_PARSED, (_evt, data) => setLevels(data.levels))
    hls.on(Hls.Events.LEVEL_SWITCHED, (_evt, data) => {
      const l = hls.levels[data.level]
      setSwitches((prev) =>
        [{ at: Date.now(), label: l ? levelLabel(l) : `level ${data.level}` }, ...prev].slice(0, 12),
      )
    })
    hls.on(Hls.Events.ERROR, (_evt, data) => {
      if (data.fatal) setError(`${data.type} — ${data.details}`)
    })

    return () => {
      hls.destroy()
      hlsRef.current = null
    }
  }, [src])

  // Sample live playback quality twice a second.
  useEffect(() => {
    const id = window.setInterval(() => {
      const video = videoRef.current
      const hls = hlsRef.current
      if (!video || !hls) return
      const bufEnd = video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0
      const q =
        typeof video.getVideoPlaybackQuality === 'function' ? video.getVideoPlaybackQuality() : null
      setStats({
        currentLevel: hls.currentLevel,
        bufferSec: Math.max(0, bufEnd - video.currentTime),
        droppedFrames: q ? q.droppedVideoFrames : 0,
        bitrateKbps: Math.round((hls.levels[hls.currentLevel]?.bitrate ?? 0) / 1000),
      })
    }, 500)
    return () => window.clearInterval(id)
  }, [])

  const pinLevel = (level: number) => {
    if (hlsRef.current) hlsRef.current.currentLevel = level
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
      <div className="space-y-3">
        <div className="overflow-hidden rounded-xl border bg-black">
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <video ref={videoRef} controls playsInline className="aspect-video w-full" />
        </div>
        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
        <div className="flex flex-wrap gap-2 text-xs">
          <Badge variant="secondary">
            level {stats.currentLevel < 0 ? 'auto' : stats.currentLevel}
          </Badge>
          <Badge variant="secondary">{stats.bitrateKbps} kbps</Badge>
          <Badge variant="secondary">{stats.bufferSec.toFixed(1)}s buffered</Badge>
          <Badge variant="secondary">{stats.droppedFrames} dropped</Badge>
        </div>
      </div>

      <div className="space-y-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Renditions</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5">
            <Button
              size="sm"
              variant={stats.currentLevel < 0 ? 'default' : 'outline'}
              className="w-full justify-start"
              onClick={() => pinLevel(-1)}
            >
              Auto (ABR)
            </Button>
            {levels.length === 0 && (
              <p className="text-muted-foreground text-xs">No master playlist loaded yet.</p>
            )}
            {levels.map((l, i) => (
              <Button
                key={i}
                size="sm"
                variant={stats.currentLevel === i ? 'default' : 'outline'}
                className="w-full justify-between font-mono"
                onClick={() => pinLevel(i)}
              >
                <span>{l.height}p</span>
                <span className="text-muted-foreground">{Math.round(l.bitrate / 1000)}k</span>
              </Button>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Switch log</CardTitle>
          </CardHeader>
          <CardContent>
            {switches.length === 0 ? (
              <p className="text-muted-foreground text-xs">Rendition switches will appear here.</p>
            ) : (
              <ol className="space-y-1 font-mono text-xs">
                {switches.map((s) => (
                  <li key={s.at} className="flex justify-between gap-2">
                    <span className="text-muted-foreground">
                      {new Date(s.at).toLocaleTimeString()}
                    </span>
                    <span>{s.label}</span>
                  </li>
                ))}
              </ol>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
