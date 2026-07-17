// The chat store: an external store (for `useSyncExternalStore`) that owns a
// single WebSocket identity ("you") talking to the pub/sub server, and shapes
// the wire protocol into a chat mental model — because a chat app *is* a
// pub/sub UI wearing a costume:
//
//  - a room            = a topic you're subscribed to
//  - sending a message = publish
//  - the message showing up in your own thread = the server fanning your
//    publish back out to you (you're a subscriber of your own room), not a
//    local echo — so nothing appears until the hub actually delivers it
//  - "who's online"    = a presence frame
//  - another tab seeing it live = fan-out, made visible without a client grid
//
// The firehose/stats below (surfaced in the Dev panel, not the main thread)
// exist for the same reason the old playground's grid did: backpressure and
// drop-shedding need *load* and a *counter* to become visible at all.
//
// Design note: under a firehose, messages can arrive far faster than React
// should re-render. Incoming frames only mutate internal runtime state and set
// a `dirty` flag; a single ~5 Hz tick rebuilds an immutable snapshot and
// notifies. That keeps the UI smooth whether it's 1 msg/s or 2000.

import type { ClientMessage, Envelope, ServerMessage } from './protocol'
import { isEnvelope } from './protocol'

export type Status = 'closed' | 'connecting' | 'open' | 'error'

export interface ThreadEntry {
  id: number
  kind: 'message' | 'system' | 'error'
  from?: string
  mine?: boolean
  text: string
  latencyMs?: number | null
  at: number
}

export interface RoomSnapshot {
  topic: string
  members: string[]
  messages: ThreadEntry[]
  droppedGaps: number
  unread: number
}

export interface LoadSnapshot {
  running: boolean
  topic: string
  rate: number
  sent: number
}

export interface Totals {
  published: number
  received: number
  droppedGaps: number
  publishRate: number
  deliverRate: number
  avgLatencyMs: number | null
}

export interface Snapshot {
  endpoint: string
  token: string
  name: string
  status: Status
  error?: string
  rooms: RoomSnapshot[]
  activeTopic: string | null
  load: LoadSnapshot
  totals: Totals
}

const FLUSH_MS = 200
const LOAD_TICK_MS = 50
const THREAD_CAP = 300
const MAX_RATE = 2000
const EMA = 0.4

let entrySeq = 1

function load<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key)
    return v === null ? fallback : (v as unknown as T)
  } catch {
    return fallback
  }
}

function randomName(): string {
  return `guest-${Math.floor(100 + Math.random() * 900)}`
}

class ChatStore {
  private listeners = new Set<() => void>()

  private ws: WebSocket | null = null
  private status: Status = 'closed'
  private error?: string

  private name = load('pubsub.name', randomName())
  private endpoint = load('pubsub.endpoint', '/ws')
  private token = load('pubsub.token', '')

  private subscriptions: string[] = [] // joined rooms, in join order
  private presence = new Map<string, string[]>() // topic -> members
  private threads = new Map<string, ThreadEntry[]>() // topic -> messages
  private droppedGaps = new Map<string, number>() // topic -> dropped count
  private unread = new Map<string, number>() // topic -> unseen count
  private seqTrack = new Map<string, number>() // `${from}::${topic}` -> highest seq seen
  private outSeq = new Map<string, number>() // topic -> next outbound seq
  private activeTopic: string | null = null

  private published = 0
  private received = 0
  private publishSample = 0
  private receivedSample = 0
  private publishRate = 0
  private deliverRate = 0
  private avgLatencyMs: number | null = null

  private loadState: LoadSnapshot = { running: false, topic: 'firehose', rate: 200, sent: 0 }
  private loadTimer: ReturnType<typeof setInterval> | null = null
  private loadCarry = 0

  private dirty = true
  private lastTickAt = performance.now()
  private snapshot: Snapshot = this.build()

  constructor() {
    setInterval(() => this.tick(), FLUSH_MS)
  }

  // ---- external-store contract ---------------------------------------------

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  getSnapshot = (): Snapshot => this.snapshot

  // ---- identity / config ------------------------------------------------------

  setName(v: string) {
    this.name = v
    try {
      localStorage.setItem('pubsub.name', v)
    } catch {
      /* ignore */
    }
    this.commit()
  }

  setEndpoint(v: string) {
    this.endpoint = v
    try {
      localStorage.setItem('pubsub.endpoint', v)
    } catch {
      /* ignore */
    }
    this.commit()
  }

  setToken(v: string) {
    this.token = v
    try {
      localStorage.setItem('pubsub.token', v)
    } catch {
      /* ignore */
    }
    this.commit()
  }

  // ---- connection lifecycle --------------------------------------------------

  connect() {
    if (this.status === 'open' || this.status === 'connecting') return
    let ws: WebSocket
    try {
      ws = new WebSocket(this.wsUrl())
    } catch (e) {
      this.status = 'error'
      this.error = String(e)
      this.commit()
      return
    }
    this.ws = ws
    this.status = 'connecting'
    this.error = undefined

    ws.onopen = () => {
      this.status = 'open'
      this.error = undefined
      // Re-join every room this identity had joined (reconnect-safe).
      for (const topic of this.subscriptions) this.send({ type: 'subscribe', topic })
      this.commit()
    }
    ws.onmessage = (ev) => this.onMessage(ev.data)
    ws.onerror = () => {
      this.status = 'error'
      this.error = 'socket error'
      this.dirty = true
    }
    ws.onclose = (ev) => {
      if (this.ws === ws) this.ws = null
      this.status = this.status === 'error' ? 'error' : 'closed'
      if (this.loadState.running) this.stopLoad()
      this.sysAll(`disconnected (code ${ev.code}${ev.reason ? ` — ${ev.reason}` : ''})`)
      this.commit()
    }
    this.commit()
  }

  disconnect() {
    this.ws?.close(1000, 'client disconnect')
    this.ws = null
    this.status = 'closed'
    this.commit()
  }

  // ---- rooms ------------------------------------------------------------------

  joinRoom(topic: string) {
    const t = topic.trim()
    if (!t || this.subscriptions.includes(t)) return
    this.subscriptions.push(t)
    if (!this.threads.has(t)) this.threads.set(t, [])
    this.sys(t, `you joined ${t}`)
    if (this.status === 'open') this.send({ type: 'subscribe', topic: t })
    if (!this.activeTopic) this.activeTopic = t
    this.commit()
  }

  leaveRoom(topic: string) {
    if (!this.subscriptions.includes(topic)) return
    this.subscriptions = this.subscriptions.filter((t) => t !== topic)
    if (this.status === 'open') this.send({ type: 'unsubscribe', topic })
    this.threads.delete(topic)
    this.presence.delete(topic)
    this.droppedGaps.delete(topic)
    this.unread.delete(topic)
    if (this.activeTopic === topic) this.activeTopic = this.subscriptions[0] ?? null
    this.commit()
  }

  setActiveTopic(topic: string) {
    if (!this.subscriptions.includes(topic)) return
    this.activeTopic = topic
    this.unread.set(topic, 0)
    this.commit()
  }

  // ---- messaging ----------------------------------------------------------------

  sendMessage(topic: string, text: string) {
    const body = text.trim()
    if (!body) return
    if (this.status !== 'open') {
      this.sys(topic, `can't send: not connected`)
      this.commit()
      return
    }
    const seq = (this.outSeq.get(topic) ?? 0) + 1
    this.outSeq.set(topic, seq)
    const env: Envelope = { seq, ts: Date.now(), from: this.name, body }
    this.send({ type: 'publish', topic, payload: env })
    this.published += 1
    this.commit()
  }

  // ---- firehose / load ---------------------------------------------------------

  configureLoad(patch: Partial<Pick<LoadSnapshot, 'topic' | 'rate'>>) {
    if (patch.rate !== undefined) patch.rate = Math.max(1, Math.min(MAX_RATE, Math.round(patch.rate)))
    this.loadState = { ...this.loadState, ...patch }
    this.commit()
  }

  startLoad() {
    if (this.loadState.running || this.status !== 'open') return
    this.loadState = { ...this.loadState, running: true, sent: 0 }
    this.loadCarry = 0
    let last = performance.now()
    this.loadTimer = setInterval(() => {
      if (this.status !== 'open') {
        this.stopLoad()
        return
      }
      const now = performance.now()
      const dt = (now - last) / 1000
      last = now
      this.loadCarry += this.loadState.rate * dt
      let n = Math.floor(this.loadCarry)
      this.loadCarry -= n
      const topic = this.loadState.topic
      while (n-- > 0) {
        const seq = (this.outSeq.get(topic) ?? 0) + 1
        this.outSeq.set(topic, seq)
        const env: Envelope = { seq, ts: Date.now(), from: this.name, body: { n: seq } }
        this.send({ type: 'publish', topic, payload: env })
        this.published += 1
        this.loadState.sent += 1
      }
      this.dirty = true
    }, LOAD_TICK_MS)
    this.commit()
  }

  stopLoad() {
    if (this.loadTimer) clearInterval(this.loadTimer)
    this.loadTimer = null
    this.loadState = { ...this.loadState, running: false }
    this.commit()
  }

  // ---- inbound frame handling ----------------------------------------------------

  private onMessage(data: unknown) {
    if (typeof data !== 'string') return
    let msg: ServerMessage
    try {
      msg = JSON.parse(data) as ServerMessage
    } catch {
      this.sysError(this.activeTopic, `unparseable frame: ${String(data).slice(0, 120)}`)
      this.dirty = true
      return
    }
    switch (msg.type) {
      case 'message': {
        this.received += 1
        let latency: number | null = null
        let from = 'unknown'
        let text: string
        let mine = false
        if (isEnvelope(msg.payload)) {
          const env = msg.payload as Envelope
          from = env.from ?? 'unknown'
          mine = from === this.name
          if (typeof env.ts === 'number') {
            latency = Date.now() - env.ts
            this.avgLatencyMs = this.avgLatencyMs === null ? latency : this.avgLatencyMs + EMA * (latency - this.avgLatencyMs)
          }
          if (typeof env.seq === 'number') {
            const key = `${from}::${msg.topic}`
            const prev = this.seqTrack.get(key)
            if (prev !== undefined && env.seq > prev + 1) {
              this.droppedGaps.set(msg.topic, (this.droppedGaps.get(msg.topic) ?? 0) + (env.seq - prev - 1))
            }
            if (prev === undefined || env.seq > prev) this.seqTrack.set(key, env.seq)
          }
          text = env.body === undefined ? JSON.stringify(env) : typeof env.body === 'string' ? env.body : JSON.stringify(env.body)
        } else {
          text = typeof msg.payload === 'string' ? msg.payload : JSON.stringify(msg.payload)
        }
        this.append(msg.topic, { kind: 'message', from, mine, text, latencyMs: latency })
        if (msg.topic !== this.activeTopic) this.unread.set(msg.topic, (this.unread.get(msg.topic) ?? 0) + 1)
        break
      }
      case 'presence': {
        this.presence.set(msg.topic, msg.members)
        this.sys(msg.topic, `${msg.members.length} present: ${msg.members.join(', ') || '—'}`)
        break
      }
      case 'error': {
        this.error = msg.reason
        this.sysError(this.activeTopic, msg.reason)
        break
      }
      default:
        this.sysError(this.activeTopic, `unknown frame: ${String(data).slice(0, 120)}`)
    }
    this.dirty = true
  }

  // ---- internals ---------------------------------------------------------------

  private send(msg: ClientMessage) {
    try {
      this.ws?.send(JSON.stringify(msg))
    } catch {
      /* socket went away between the guard and here; onclose will clean up */
    }
  }

  private wsUrl(): string {
    let base: string
    const ep = this.endpoint.trim() || '/ws'
    if (/^wss?:\/\//i.test(ep)) {
      base = ep
    } else {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      base = `${proto}//${location.host}${ep.startsWith('/') ? '' : '/'}${ep}`
    }
    if (this.token.trim()) base += (base.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(this.token.trim())
    return base
  }

  private sys(topic: string, text: string) {
    this.append(topic, { kind: 'system', text })
  }

  private sysError(topic: string | null, text: string) {
    if (!topic) return
    this.append(topic, { kind: 'error', text })
  }

  private sysAll(text: string) {
    for (const t of this.subscriptions) this.sys(t, text)
  }

  private append(topic: string, e: Omit<ThreadEntry, 'id' | 'at'>) {
    const list = this.threads.get(topic) ?? []
    list.push({ ...e, id: entrySeq++, at: Date.now() })
    if (list.length > THREAD_CAP) list.splice(0, list.length - THREAD_CAP)
    this.threads.set(topic, list)
  }

  private tick() {
    const now = performance.now()
    const dt = (now - this.lastTickAt) / 1000
    this.lastTickAt = now
    let active = false
    if (dt > 0) {
      const rinst = (this.received - this.receivedSample) / dt
      this.receivedSample = this.received
      this.deliverRate += EMA * (rinst - this.deliverRate)
      if (Math.round(this.deliverRate) > 0) active = true

      const pinst = (this.published - this.publishSample) / dt
      this.publishSample = this.published
      this.publishRate += EMA * (pinst - this.publishRate)
      if (Math.round(this.publishRate) > 0) active = true
    }
    if (this.dirty || active) this.commit()
  }

  private commit() {
    this.dirty = false
    this.snapshot = this.build()
    for (const fn of this.listeners) fn()
  }

  private build(): Snapshot {
    const rooms: RoomSnapshot[] = this.subscriptions.map((topic) => ({
      topic,
      members: this.presence.get(topic) ?? [],
      messages: this.threads.get(topic) ?? [],
      droppedGaps: this.droppedGaps.get(topic) ?? 0,
      unread: this.unread.get(topic) ?? 0,
    }))

    return {
      endpoint: this.endpoint,
      token: this.token,
      name: this.name,
      status: this.status,
      error: this.error,
      rooms,
      activeTopic: this.activeTopic,
      load: { ...this.loadState },
      totals: {
        published: this.published,
        received: this.received,
        droppedGaps: rooms.reduce((a, r) => a + r.droppedGaps, 0),
        publishRate: Math.max(0, Math.round(this.publishRate)),
        deliverRate: Math.max(0, Math.round(this.deliverRate)),
        avgLatencyMs: this.avgLatencyMs,
      },
    }
  }
}

export const store = new ChatStore()
