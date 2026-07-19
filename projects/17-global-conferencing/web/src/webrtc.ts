// The browser side of the conference. Camera + the simulcast announcement are
// wired; the media path reuses the project-15 SFU, so — as in 15 — establishing
// it is your work. Project 17 adds the *federation* on top (which region anchors
// the room, when a cascade leg is needed), and that lives in the Rust backend.

import type { SimulcastLayer } from '@/api'

/** Open the local camera + mic. Fully wired. */
export async function openCamera(): Promise<MediaStream> {
  return navigator.mediaDevices.getUserMedia({
    video: { width: { ideal: 1280 }, height: { ideal: 720 } },
    audio: true,
  })
}

/** A short random ICE ufrag for the client side of the credential exchange. */
export function randomUfrag(): string {
  return Math.random().toString(36).slice(2, 10)
}

/** The conventional three-rung simulcast ladder (full / half / quarter). */
export function proposeLayers(): SimulcastLayer[] {
  return [
    { rid: 'f', ssrc: 0, bitrate_bps: 1_200_000 },
    { rid: 'h', ssrc: 0, bitrate_bps: 500_000 },
    { rid: 'q', ssrc: 0, bitrate_bps: 150_000 },
  ]
}

/**
 * ─── THE MEAT — yours to build (same as project 15) ─────────────────────────
 * publish/subscribe return an ICE-cred + media-address handle, not an SDP
 * answer. Bridge a browser `RTCPeerConnection` to it (ICE, DTLS, SRTP, simulcast)
 * to bring media up. In 17 the *placement/cascade* decisions are already made by
 * the backend — this call is unchanged from the single-region SFU.
 */
export async function connectMedia(local: MediaStream, handle: Record<string, unknown>): Promise<RTCPeerConnection> {
  void local
  void handle
  throw new Error(
    'connectMedia() is your TODO — reuse the project-15 RTCPeerConnection ↔ SFU bridge here.',
  )
}
