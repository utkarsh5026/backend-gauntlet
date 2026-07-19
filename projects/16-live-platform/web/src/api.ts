// Typed client for the project-16 live platform. One HTTP origin fronts playback,
// chat, ingest, and status; Vite proxies them to the Rust backend.

/** Mirrors `chat::ChatMessage` in the backend — the JSON framing over the WS. */
export type ChatMessage = {
  stream_key: string
  user: string
  body: string
  sent_at_ms: number
}

/** Loose view of `GET /status` (admin.rs). Fields firm up as you build the platform. */
export type PlatformStatus = {
  streams_live: number
  streams: unknown[]
  chat: { active_channels: number }
  transcode: { queue_depth: number; max_replicas: number }
  [k: string]: unknown
}

export async function fetchStatus(): Promise<PlatformStatus> {
  const res = await fetch('/status')
  if (!res.ok) throw new Error(`GET /status → ${res.status}`)
  return (await res.json()) as PlatformStatus
}

/** The ABR master playlist a viewer's player picks a rendition from. */
export function masterPlaylistUrl(stream: string): string {
  return `/live/${encodeURIComponent(stream)}/master.m3u8`
}

/** The per-channel chat + presence WebSocket. */
export function chatSocketUrl(stream: string): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/chat/${encodeURIComponent(stream)}/ws`
}
