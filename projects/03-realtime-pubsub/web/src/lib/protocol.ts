// The wire protocol, mirrored from the Rust `protocol.rs`. Both enums are
// internally tagged by a snake_case `type` field, e.g.
//   {"type":"subscribe","topic":"room1"}
//   {"type":"publish","topic":"room1","payload":{...}}
// Keep these in lockstep with projects/03-realtime-pubsub/src/protocol.rs.

/** Messages a client sends *to* the server. */
export type ClientMessage =
  | { type: 'subscribe'; topic: string }
  | { type: 'unsubscribe'; topic: string }
  | { type: 'publish'; topic: string; payload: unknown }

/** Messages the server sends *to* a client. */
export type ServerMessage =
  | { type: 'message'; topic: string; payload: unknown }
  | { type: 'presence'; topic: string; members: string[] }
  | { type: 'error'; reason: string }

/**
 * The playground stamps every published payload with this envelope so a
 * subscriber can measure end-to-end latency (`ts`) and detect *dropped* messages
 * as gaps in the per-sender sequence (`seq`). Both are optional on read: a payload
 * that doesn't carry them (e.g. one you hand-craft) is still delivered and shown,
 * it just won't contribute to the latency/drop stats.
 */
export interface Envelope {
  /** Monotonic per (sender, topic). A hole in the received run = a dropped message. */
  seq?: number
  /** `Date.now()` at publish. Same-clock across tabs → true end-to-end latency. */
  ts?: number
  /** Display name of the sending client (never trusted for anything but display). */
  from?: string
  /** The actual user payload. */
  body?: unknown
}

export function isEnvelope(v: unknown): v is Envelope {
  return typeof v === 'object' && v !== null && ('seq' in v || 'ts' in v || 'from' in v)
}
