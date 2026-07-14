// The playground store: an external store (for `useSyncExternalStore`) that owns
// a fleet of live WebSocket connections to the pub/sub server and derives the
// stats that make the SPEC's hard parts *visible* from the browser:
//
//  - fan-out       — one publish lights up every subscriber of the topic at once,
//                    and `deliverRate` = publishRate × subscribers-per-topic.
//  - backpressure  — each publish carries a per-sender `seq`; a subscriber that
//                    the server sheds under its overflow policy sees a HOLE in the
//                    run, which we count as `droppedGaps`. That counter is the
//                    client-side mirror of the server's drop metric (V2's payoff).
//  - latency       — every payload carries `ts`; since all tabs share one clock,
//                    `now - ts` is a true end-to-end delivery latency.
//
// Design note: under a firehose, messages arrive far faster than React should
// re-render. Incoming frames only mutate internal runtime objects and set a
// `dirty` flag; a single ~5 Hz tick rebuilds an immutable snapshot and notifies.
// That keeps the UI smooth whether it's 1 msg/s or 2000.

import type { ClientMessage, Envelope, ServerMessage } from './protocol'
import { isEnvelope } from './protocol'

export type Status = 'closed' | 'connecting' | 'open' | 'error'

export interface LogEntry {
  id: number
  dir: 'in' | 'out'
  kind: 'message' | 'presence' | 'error' | 'system'
  topic?: string
  from?: string
  text: string
  latencyMs?: number
  at: number
}

export interface ClientSnapshot {
  id: string
  name: string
  status: Status
  error?: string
  subscriptions: string[]
  received: number
  published: number
  droppedGaps: number
  lastLatencyMs: number | null
  avgLatencyMs: number | null
  ratePerSec: number
  log: LogEntry[]
}

export interface RoomSnapshot {
  topic: string
  members: string[]
  subscriberIds: string[]
  delivered: number
}

export interface LoadSnapshot {
  running: boolean
  senderId: string | null
  topic: string
  rate: number
  sent: number
}

export interface Totals {
  clients: number
  connected: number
  topics: number
  published: number
  delivered: number
  dropped: number
  publishRate: number
  deliverRate: number
}

export interface Snapshot {
  endpoint: string
  token: string
  clients: ClientSnapshot[]
  rooms: RoomSnapshot[]
  load: LoadSnapshot
  totals: Totals
}

const FLUSH_MS = 200
const LOAD_TICK_MS = 50
const LOG_CAP = 60
const MAX_RATE = 2000
const EMA = 0.4

// ---- internal, mutable runtime (never handed to React directly) -------------

interface ClientRuntime {
  id: string
  name: string
  ws: WebSocket | null
  status: Status
  error?: string
  subscriptions: Set<string>
  received: number
  published: number
  droppedGaps: number
  lastLatencyMs: number | null
  avgLatencyMs: number | null
  ratePerSec: number
  rateSample: number
  seqTrack: Map<string, number> // `${from}::${topic}` -> highest seq seen
  outSeq: Map<string, number> // topic -> next outbound seq for this sender
  log: LogEntry[]
}

let logSeq = 1
let clientSeq = 1

function load<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key)
    return v === null ? fallback : (v as unknown as T)
  } catch {
    return fallback
  }
}

class PlaygroundStore {
  private listeners = new Set<() => void>()
  private clients: ClientRuntime[] = []
  private endpoint = load('pubsub.endpoint', '/ws')
  private token = load('pubsub.token', '')

  private presence = new Map<string, string[]>() // topic -> members
  private delivered = new Map<string, number>() // topic -> delivered count

  private published = 0
  private publishSample = 0
  private publishRate = 0

  private loadState: LoadSnapshot = { running: false, senderId: null, topic: 'firehose', rate: 200, sent: 0 }
  private loadTimer: ReturnType<typeof setInterval> | null = null
  private loadCarry = 0

  private dirty = true
  private lastTickAt = performance.now()
  private snapshot: Snapshot = this.build()

  constructor() {
    setInterval(() => this.tick(), FLUSH_MS)
    // A couple of clients ready to go, so the page isn't empty on first load.
    this.addClient('alice')
    this.addClient('bob')
  }

  // ---- external-store contract ---------------------------------------------

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  getSnapshot = (): Snapshot => this.snapshot

  // ---- config ---------------------------------------------------------------

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

  // ---- clients ---------------------------------------------------------------

  addClient(name?: string): string {
    const id = `c${clientSeq++}`
    this.clients.push({
      id,
      name: name ?? `client-${id}`,
      ws: null,
      status: 'closed',
      subscriptions: new Set(),
      received: 0,
      published: 0,
      droppedGaps: 0,
      lastLatencyMs: null,
      avgLatencyMs: null,
      ratePerSec: 0,
      rateSample: 0,
      seqTrack: new Map(),
      outSeq: new Map(),
      log: [],
    })
    this.commit()
    return id
  }

  spawn(n: number) {
    for (let i = 0; i < n; i++) this.addClient()
    this.commit()
  }

  removeClient(id: string) {
    const c = this.find(id)
    if (c) c.ws?.close(1000, 'removed')
    if (this.loadState.senderId === id) this.stopLoad()
    this.clients = this.clients.filter((c) => c.id !== id)
    this.commit()
  }

  renameClient(id: string, name: string) {
    const c = this.find(id)
    if (c) c.name = name
    this.commit()
  }

  clearLog(id: string) {
    const c = this.find(id)
    if (c) c.log = []
    this.commit()
  }

  // ---- connection lifecycle --------------------------------------------------

  connect(id: string) {
    const c = this.find(id)
    if (!c || c.status === 'open' || c.status === 'connecting') return
    let ws: WebSocket
    try {
      ws = new WebSocket(this.wsUrl())
    } catch (e) {
      c.status = 'error'
      c.error = String(e)
      this.commit()
      return
    }
    c.ws = ws
    c.status = 'connecting'
    c.error = undefined

    ws.onopen = () => {
      c.status = 'open'
      c.error = undefined
      this.sys(c, `connected → ${this.wsUrl()}`)
      // Re-establish any topics this client had joined (reconnect-safe).
      for (const topic of c.subscriptions) this.send(c, { type: 'subscribe', topic })
      this.commit()
    }
    ws.onmessage = (ev) => this.onMessage(c, ev.data)
    ws.onerror = () => {
      c.status = 'error'
      c.error = 'socket error'
      this.dirty = true
    }
    ws.onclose = (ev) => {
      if (c.ws === ws) c.ws = null
      c.status = c.status === 'error' ? 'error' : 'closed'
      this.sys(c, `disconnected (code ${ev.code}${ev.reason ? ` — ${ev.reason}` : ''})`)
      if (this.loadState.senderId === id) this.stopLoad()
      this.commit()
    }
    this.commit()
  }

  disconnect(id: string) {
    const c = this.find(id)
    if (!c) return
    c.ws?.close(1000, 'client disconnect')
    c.ws = null
    c.status = 'closed'
    this.commit()
  }

  connectAll() {
    for (const c of this.clients) this.connect(c.id)
  }

  disconnectAll() {
    for (const c of this.clients) this.disconnect(c.id)
  }

  // ---- protocol actions ------------------------------------------------------

  subscribeTopic(id: string, topic: string) {
    const t = topic.trim()
    const c = this.find(id)
    if (!c || !t) return
    c.subscriptions.add(t)
    // Fresh subscription → forget any stale per-sender seq for this topic so we
    // don't count a spurious gap from a previous run.
    for (const key of [...c.seqTrack.keys()]) if (key.endsWith(`::${t}`)) c.seqTrack.delete(key)
    if (c.status === 'open') this.send(c, { type: 'subscribe', topic: t })
    this.commit()
  }

  unsubscribeTopic(id: string, topic: string) {
    const c = this.find(id)
    if (!c) return
    c.subscriptions.delete(topic)
    if (c.status === 'open') this.send(c, { type: 'unsubscribe', topic })
    this.commit()
  }

  subscribeAll(topic: string) {
    const t = topic.trim()
    if (!t) return
    for (const c of this.clients) if (c.status === 'open') this.subscribeTopic(c.id, t)
    this.commit()
  }

  /** Publish a user `body` to `topic` through client `id`, stamped for stats. */
  publish(id: string, topic: string, body: unknown) {
    const c = this.find(id)
    const t = topic.trim()
    if (!c || !t) return
    if (c.status !== 'open') {
      this.sys(c, `can't publish to "${t}": not connected`)
      this.commit()
      return
    }
    const seq = (c.outSeq.get(t) ?? 0) + 1
    c.outSeq.set(t, seq)
    const env: Envelope = { seq, ts: Date.now(), from: c.name, body }
    this.send(c, { type: 'publish', topic: t, payload: env })
    c.published += 1
    this.published += 1
    this.log(c, {
      dir: 'out',
      kind: 'message',
      topic: t,
      from: c.name,
      text: typeof body === 'string' ? body : JSON.stringify(body),
    })
    this.commit()
  }

  // ---- firehose / load -------------------------------------------------------

  configureLoad(patch: Partial<Pick<LoadSnapshot, 'senderId' | 'topic' | 'rate'>>) {
    if (patch.rate !== undefined) patch.rate = Math.max(1, Math.min(MAX_RATE, Math.round(patch.rate)))
    this.loadState = { ...this.loadState, ...patch }
    this.commit()
  }

  startLoad() {
    if (this.loadState.running) return
    const sender = this.loadState.senderId ?? this.clients.find((c) => c.status === 'open')?.id ?? null
    if (!sender) return
    this.loadState = { ...this.loadState, running: true, senderId: sender, sent: 0 }
    this.loadCarry = 0
    let last = performance.now()
    this.loadTimer = setInterval(() => {
      const c = this.find(this.loadState.senderId ?? '')
      if (!c || c.status !== 'open') {
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
        const seq = (c.outSeq.get(topic) ?? 0) + 1
        c.outSeq.set(topic, seq)
        const env: Envelope = { seq, ts: Date.now(), from: c.name, body: { n: seq } }
        this.send(c, { type: 'publish', topic, payload: env })
        c.published += 1
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

  // ---- inbound frame handling ------------------------------------------------

  private onMessage(c: ClientRuntime, data: unknown) {
    if (typeof data !== 'string') return
    let msg: ServerMessage
    try {
      msg = JSON.parse(data) as ServerMessage
    } catch {
      this.log(c, { dir: 'in', kind: 'error', text: `unparseable frame: ${String(data).slice(0, 120)}` })
      this.dirty = true
      return
    }
    switch (msg.type) {
      case 'message': {
        c.received += 1
        this.delivered.set(msg.topic, (this.delivered.get(msg.topic) ?? 0) + 1)
        let latency: number | undefined
        let from: string | undefined
        let text: string
        if (isEnvelope(msg.payload)) {
          const env = msg.payload as Envelope
          from = env.from
          if (typeof env.ts === 'number') {
            latency = Date.now() - env.ts
            c.lastLatencyMs = latency
            c.avgLatencyMs = c.avgLatencyMs === null ? latency : c.avgLatencyMs + EMA * (latency - c.avgLatencyMs)
          }
          if (typeof env.seq === 'number' && from) {
            const key = `${from}::${msg.topic}`
            const prev = c.seqTrack.get(key)
            if (prev !== undefined && env.seq > prev + 1) c.droppedGaps += env.seq - prev - 1
            if (prev === undefined || env.seq > prev) c.seqTrack.set(key, env.seq)
          }
          text = env.body === undefined ? JSON.stringify(env) : typeof env.body === 'string' ? env.body : JSON.stringify(env.body)
        } else {
          text = typeof msg.payload === 'string' ? msg.payload : JSON.stringify(msg.payload)
        }
        this.log(c, { dir: 'in', kind: 'message', topic: msg.topic, from, text, latencyMs: latency })
        break
      }
      case 'presence': {
        this.presence.set(msg.topic, msg.members)
        this.log(c, { dir: 'in', kind: 'presence', topic: msg.topic, text: `${msg.members.length} present: ${msg.members.join(', ') || '—'}` })
        break
      }
      case 'error': {
        this.log(c, { dir: 'in', kind: 'error', text: msg.reason })
        break
      }
      default:
        this.log(c, { dir: 'in', kind: 'system', text: `unknown frame: ${data.slice(0, 120)}` })
    }
    this.dirty = true
  }

  // ---- internals -------------------------------------------------------------

  private find(id: string): ClientRuntime | undefined {
    return this.clients.find((c) => c.id === id)
  }

  private send(c: ClientRuntime, msg: ClientMessage) {
    try {
      c.ws?.send(JSON.stringify(msg))
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

  private sys(c: ClientRuntime, text: string) {
    this.log(c, { dir: 'in', kind: 'system', text })
  }

  private log(c: ClientRuntime, e: Omit<LogEntry, 'id' | 'at'>) {
    c.log.unshift({ ...e, id: logSeq++, at: Date.now() })
    if (c.log.length > LOG_CAP) c.log.length = LOG_CAP
  }

  private tick() {
    const now = performance.now()
    const dt = (now - this.lastTickAt) / 1000
    this.lastTickAt = now
    let active = false
    if (dt > 0) {
      for (const c of this.clients) {
        const inst = (c.received - c.rateSample) / dt
        c.rateSample = c.received
        c.ratePerSec += EMA * (inst - c.ratePerSec)
        if (Math.round(c.ratePerSec) > 0) active = true
      }
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
    const clients: ClientSnapshot[] = this.clients.map((c) => ({
      id: c.id,
      name: c.name,
      status: c.status,
      error: c.error,
      subscriptions: [...c.subscriptions].sort(),
      received: c.received,
      published: c.published,
      droppedGaps: c.droppedGaps,
      lastLatencyMs: c.lastLatencyMs,
      avgLatencyMs: c.avgLatencyMs,
      ratePerSec: Math.max(0, Math.round(c.ratePerSec)),
      log: c.log,
    }))

    const topics = new Set<string>()
    for (const c of this.clients) for (const t of c.subscriptions) topics.add(t)
    for (const t of this.presence.keys()) topics.add(t)
    for (const t of this.delivered.keys()) topics.add(t)
    const rooms: RoomSnapshot[] = [...topics]
      .sort()
      .map((topic) => ({
        topic,
        members: this.presence.get(topic) ?? [],
        subscriberIds: this.clients.filter((c) => c.subscriptions.has(topic)).map((c) => c.id),
        delivered: this.delivered.get(topic) ?? 0,
      }))

    return {
      endpoint: this.endpoint,
      token: this.token,
      clients,
      rooms,
      load: { ...this.loadState },
      totals: {
        clients: this.clients.length,
        connected: this.clients.filter((c) => c.status === 'open').length,
        topics: rooms.length,
        published: this.published,
        delivered: clients.reduce((a, c) => a + c.received, 0),
        dropped: clients.reduce((a, c) => a + c.droppedGaps, 0),
        publishRate: Math.max(0, Math.round(this.publishRate)),
        deliverRate: clients.reduce((a, c) => a + c.ratePerSec, 0),
      },
    }
  }
}

export const store = new PlaygroundStore()
