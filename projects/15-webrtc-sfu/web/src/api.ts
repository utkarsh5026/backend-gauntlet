// HTTP signaling client for the project-15 SFU. These calls are plain fetch —
// they are wired and work against the running backend. Turning the returned
// PeerHandle into an actual media flow (ICE → DTLS → SRTP) is the part you build;
// see webrtc.ts.

export type TopoPeer = { id: number; role: string }
export type TopoRoom = { room: string; peers: TopoPeer[] }
export type Topology = { rooms: TopoRoom[] }

/** One announced simulcast encoding — mirrors the backend's `SimulcastLayer`. */
export type SimulcastLayer = { rid: string; ssrc: number; bitrate_bps: number }

/** Response of publish/subscribe — ICE creds + the media address to ICE-connect to. */
export type PeerHandle = {
  peer_id: number
  ice_ufrag: string
  ice_pwd: string
  media_addr: string
  out_ssrc: number | null
}

/** `GET /rooms` — live topology. */
export async function fetchTopology(): Promise<Topology> {
  const res = await fetch('/rooms')
  if (!res.ok) throw new Error(`GET /rooms → ${res.status}`)
  return (await res.json()) as Topology
}

/** `POST /rooms/:room/publish` — announce simulcast layers, get ICE creds back. */
export async function publish(
  room: string,
  layers: SimulcastLayer[],
  clientUfrag: string,
): Promise<PeerHandle> {
  const res = await fetch(`/rooms/${encodeURIComponent(room)}/publish`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ layers, client_ufrag: clientUfrag }),
  })
  if (!res.ok) throw new Error(`POST publish → ${res.status} ${await res.text()}`)
  return (await res.json()) as PeerHandle
}

/** `POST /rooms/:room/subscribe` — attach to a publisher, get its stable SSRC. */
export async function subscribe(
  room: string,
  publisher: number,
  clientUfrag: string,
): Promise<PeerHandle> {
  const res = await fetch(`/rooms/${encodeURIComponent(room)}/subscribe`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ publisher, client_ufrag: clientUfrag }),
  })
  if (!res.ok) throw new Error(`POST subscribe → ${res.status} ${await res.text()}`)
  return (await res.json()) as PeerHandle
}
