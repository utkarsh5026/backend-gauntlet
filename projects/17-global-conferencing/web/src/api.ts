// HTTP signaling client for the project-17 cascaded SFU. `GET /rooms` reflects
// the replicated placement map (V1 consensus) — so this one node reports the
// whole cluster's view. publish/subscribe work as fetch calls; the media flow is
// the reused project-15 SFU work (see webrtc.ts).

/** Mirrors `placement::RoomPlacement`. `active_regions` is a set → JSON array. */
export type RoomPlacement = {
  room_id: string
  home_region: string
  active_regions: string[]
  epoch: number
}

/** Mirrors `cascade::RelayLink` — one inter-SFU backbone leg. */
export type RelayLink = {
  region: string
  remote_addr: string
  tracks: number
}

/** `GET /rooms` — this node's region + the global topology it agrees on. */
export type GlobalTopology = {
  region: string
  rooms: RoomPlacement[]
  relay_legs: RelayLink[]
}

export type SimulcastLayer = { rid: string; ssrc: number; bitrate_bps: number }

export async function fetchTopology(): Promise<GlobalTopology> {
  const res = await fetch('/rooms')
  if (!res.ok) throw new Error(`GET /rooms → ${res.status}`)
  return (await res.json()) as GlobalTopology
}

/** `POST /rooms/:room/publish` — first publish PLACES the room (picks its home region). */
export async function publish(
  room: string,
  layers: SimulcastLayer[],
  clientUfrag: string,
): Promise<Record<string, unknown>> {
  const res = await fetch(`/rooms/${encodeURIComponent(room)}/publish`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ layers, client_ufrag: clientUfrag }),
  })
  if (!res.ok) throw new Error(`POST publish → ${res.status} ${await res.text()}`)
  return (await res.json()) as Record<string, unknown>
}

/** `POST /rooms/:room/subscribe` — attach to a publisher; ensures a cascade leg if remote. */
export async function subscribe(
  room: string,
  publisher: number,
  clientUfrag: string,
): Promise<Record<string, unknown>> {
  const res = await fetch(`/rooms/${encodeURIComponent(room)}/subscribe`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ publisher, client_ufrag: clientUfrag }),
  })
  if (!res.ok) throw new Error(`POST subscribe → ${res.status} ${await res.text()}`)
  return (await res.json()) as Record<string, unknown>
}
