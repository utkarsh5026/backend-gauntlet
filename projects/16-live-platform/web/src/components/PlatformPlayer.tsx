import Hls from 'hls.js'
import { useEffect, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'

/**
 * The viewer side of "glass to glass": an LL-HLS player on the ABR master
 * playlist. Kept deliberately small — project 16 is about composing ingest,
 * transcode, packaging and edge, so this page just proves the last hop plays.
 */
export function PlatformPlayer({ src }: { src: string }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [state, setState] = useState<'idle' | 'live' | 'error'>('idle')
  const [detail, setDetail] = useState<string>('')

  useEffect(() => {
    const video = videoRef.current
    if (!video || !src) return
    setState('idle')
    setDetail('')

    if (!Hls.isSupported()) {
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src
        video.addEventListener('playing', () => setState('live'), { once: true })
        return
      }
      setState('error')
      setDetail('No MSE or native HLS in this browser.')
      return
    }

    const hls = new Hls({ lowLatencyMode: true, enableWorker: true })
    hls.loadSource(src)
    hls.attachMedia(video)
    hls.on(Hls.Events.FRAG_BUFFERED, () => setState('live'))
    hls.on(Hls.Events.ERROR, (_evt, data) => {
      if (data.fatal) {
        setState('error')
        setDetail(`${data.type} — ${data.details}`)
      }
    })
    return () => hls.destroy()
  }, [src])

  return (
    <div className="space-y-2">
      <div className="relative overflow-hidden rounded-xl border bg-black">
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <video ref={videoRef} controls playsInline autoPlay muted className="aspect-video w-full" />
        <div className="absolute left-3 top-3">
          {state === 'live' && (
            <Badge variant="destructive" className="gap-1.5">
              <span className="inline-block size-2 rounded-full bg-current" /> LIVE
            </Badge>
          )}
          {state === 'idle' && <Badge variant="secondary">connecting…</Badge>}
          {state === 'error' && <Badge variant="destructive">error</Badge>}
        </div>
      </div>
      {state === 'error' && <p className="text-destructive text-xs">{detail}</p>}
    </div>
  )
}
