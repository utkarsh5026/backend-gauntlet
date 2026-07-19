// Typed client for the project-13 LL-HLS delivery plane. Vite proxies /live to
// the Rust backend (see vite.config.ts). RTMP ingest lives on a separate TCP
// port a broadcaster (OBS/ffmpeg) pushes to — never the browser.

/** `GET /live` — the stream keys currently on air. */
export async function fetchLiveKeys(): Promise<string[]> {
  const res = await fetch('/live')
  if (!res.ok) throw new Error(`GET /live → ${res.status}`)
  const body = (await res.json()) as { live: string[] }
  return body.live ?? []
}

/** The LL-HLS media playlist URL a low-latency player loads. */
export function mediaPlaylistUrl(key: string): string {
  return `/live/${encodeURIComponent(key)}/index.m3u8`
}
