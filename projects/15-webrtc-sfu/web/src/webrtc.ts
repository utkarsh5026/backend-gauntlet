// The browser side of the SFU. getUserMedia + the simulcast announcement are
// wired; establishing the actual media path is deliberately left as your work,
// because that IS the project (ICE/STUN in V1, RTP forwarding in V3).

import type { PeerHandle, SimulcastLayer } from '@/api'

/** Open the local camera + mic. Fully wired — the local tile lights up at once. */
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

/**
 * The conventional three-rung simulcast ladder (full / half / quarter). The real
 * SSRCs are minted by the browser's RTCPeerConnection once you create it with
 * `sendEncodings`; these zeros are placeholders that announce the *shape* to the
 * SFU so its layer tables populate.
 */
export function proposeLayers(): SimulcastLayer[] {
  return [
    { rid: 'f', ssrc: 0, bitrate_bps: 1_200_000 },
    { rid: 'h', ssrc: 0, bitrate_bps: 500_000 },
    { rid: 'q', ssrc: 0, bitrate_bps: 150_000 },
  ]
}

/**
 * ─── THE MEAT — yours to build ──────────────────────────────────────────────
 * This SFU does not speak SDP offer/answer: publish/subscribe hand back raw ICE
 * credentials + a media UDP address. Making a browser `RTCPeerConnection`
 * actually ICE-connect to that address, finish DTLS, and send/receive SRTP
 * (with simulcast `sendEncodings`) is the interop challenge in the SPEC. Wire it
 * here — until then it throws and remote tiles stay dark.
 */
export async function connectMedia(local: MediaStream, handle: PeerHandle): Promise<RTCPeerConnection> {
  void local
  void handle
  throw new Error(
    'connectMedia() is your TODO — bridge RTCPeerConnection to the SFU signaling (SPEC V1 ICE/STUN, V3 forwarding).',
  )
}
