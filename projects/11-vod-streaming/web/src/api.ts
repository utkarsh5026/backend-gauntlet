// Typed client for the project-11 VOD server. Requests go to same-origin paths;
// Vite proxies /vod + /assets to the Rust backend (see vite.config.ts).

export type Asset = {
  asset: string
  /** Rendition ids (e.g. "720p", "480p"), or null before the ladder is built. */
  renditions: string[] | null
}

/** `GET /assets` — the loaded library (asset → rendition ids). */
export async function fetchAssets(): Promise<Asset[]> {
  const res = await fetch('/assets')
  if (!res.ok) throw new Error(`GET /assets → ${res.status}`)
  const body = (await res.json()) as { assets: Asset[] }
  return body.assets ?? []
}

/** The HLS master playlist URL a player loads to start adaptive playback. */
export function masterPlaylistUrl(asset: string): string {
  return `/vod/${encodeURIComponent(asset)}/master.m3u8`
}
