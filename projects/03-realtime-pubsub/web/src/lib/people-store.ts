// The PEOPLE STORE — the *live* half of the admin panel.
//
// directory.ts owns the persistent roster (REST). This owns the ephemeral
// reality: for each person you bring ONLINE, it opens a real WebSocket carrying
// that person's identity + token and subscribes it to that person's groups.
// OFFLINE = no socket. This is "a person is two things in two places" as code
// (see SPEC V3 — presence is soft state).
//
// It's an external store (useSyncExternalStore), the same pattern as the single-
// identity `store.ts`, generalized from one "you" to a Map<personId, PersonConn>.

import type { Group, Membership, Person } from './directory'
import { directoryApi } from './directory'

export type PersonStatus = 'offline' | 'connecting' | 'online' | 'error'

/** localStorage read with a fallback, shared with store.ts (`pubsub.*` keys). */
function loadLS(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback
  } catch {
    return fallback
  }
}

function saveLS(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* ignore */
  }
}

/** One person's live connection: a single WebSocket + the topics it should join. */
class PersonConn {
  readonly person: Person
  status: PersonStatus = 'offline'

  private ws: WebSocket | null = null
  private topics: string[] = []
  private token = ''
  private endpoint = '/ws'
  private readonly onChange: () => void

  constructor(person: Person, onChange: () => void) {
    this.person = person
    this.onChange = onChange
  }

  /** Open this person's socket and subscribe it to `topics`. The identity rides
   *  `?identity=<name>` so this person shows up by name in the real presence
   *  roster (backend: routes.rs `resolve_identity` → `presence.join`). */
  online(topics: string[], token: string, endpoint: string): void {
    if (this.status === 'connecting' || this.status === 'online') return
    this.topics = topics
    this.token = token
    this.endpoint = endpoint

    let ws: WebSocket
    try {
      ws = new WebSocket(this.wsUrl())
    } catch {
      this.status = 'error'
      this.onChange()
      return
    }
    this.ws = ws
    this.status = 'connecting'
    this.onChange()

    ws.onopen = () => {
      this.status = 'online'
      for (const t of this.topics) this.send({ type: 'subscribe', topic: t })
      this.onChange()
    }
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null
      // A close during 'connecting' (e.g. 401 bad token) surfaces as 'error';
      // a close while online is a normal offline.
      this.status = this.status === 'connecting' ? 'error' : 'offline'
      this.onChange()
    }
    ws.onerror = () => {
      this.status = 'error'
      this.onChange()
    }
    // The admin panel doesn't render inbound frames, so ws.onmessage is unused.
  }

  /** Close this person's socket. */
  offline(): void {
    this.ws?.close(1000, 'admin: offline')
    this.ws = null
    this.status = 'offline'
    this.onChange()
  }

  /** Add/remove a live subscription when a membership changes while online. */
  join(topic: string): void {
    if (!this.topics.includes(topic)) this.topics.push(topic)
    if (this.status === 'online') this.send({ type: 'subscribe', topic })
  }
  leave(topic: string): void {
    this.topics = this.topics.filter((t) => t !== topic)
    if (this.status === 'online') this.send({ type: 'unsubscribe', topic })
  }

  private send(msg: unknown): void {
    try {
      this.ws?.send(JSON.stringify(msg))
    } catch {
      /* socket went away between the guard and here; onclose will clean up */
    }
  }

  /** Build the ws URL: identity + token on the query string (browsers can't set
   *  handshake headers). */
  private wsUrl(): string {
    const ep = this.endpoint.trim() || '/ws'
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const base = /^wss?:\/\//i.test(ep)
      ? ep
      : `${proto}//${location.host}${ep.startsWith('/') ? '' : '/'}${ep}`
    const params = new URLSearchParams({ identity: this.person.name })
    if (this.token.trim()) params.set('token', this.token.trim())
    return `${base}${base.includes('?') ? '&' : '?'}${params.toString()}`
  }
}

export interface PeopleSnapshot {
  people: Person[]
  groups: Group[]
  memberships: Membership[]
  /** personId → live status. */
  status: Record<string, PersonStatus>
  token: string
  endpoint: string
}

class PeopleStore {
  private listeners = new Set<() => void>()
  private conns = new Map<string, PersonConn>()

  private people: Person[] = []
  private groups: Group[] = []
  private memberships: Membership[] = []
  private token = loadLS('pubsub.token', '')
  private endpoint = loadLS('pubsub.endpoint', '/ws')

  private snapshot: PeopleSnapshot = this.build()

  // ---- external-store contract ---------------------------------------------

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }
  getSnapshot = (): PeopleSnapshot => this.snapshot

  // ---- roster (persistent, via REST) ---------------------------------------

  /** Load people/groups/memberships from the backend. Call once on mount. */
  async refresh(): Promise<void> {
    const [people, groups, memberships] = await Promise.all([
      directoryApi.listPeople(),
      directoryApi.listGroups(),
      directoryApi.listMemberships(),
    ])
    this.people = people
    this.groups = groups
    this.memberships = memberships
    // Keep a PersonConn per person; drop connections for deleted people.
    for (const p of people) {
      if (!this.conns.has(p.id)) this.conns.set(p.id, new PersonConn(p, () => this.commit()))
    }
    for (const id of [...this.conns.keys()]) {
      if (!people.some((p) => p.id === id)) this.conns.delete(id)
    }
    this.commit()
  }

  async createPerson(name: string, emoji: string, color: string): Promise<void> {
    await directoryApi.createPerson(name, emoji, color)
    await this.refresh()
  }
  async createGroup(name: string, emoji: string, color: string): Promise<void> {
    await directoryApi.createGroup(name, emoji, color)
    await this.refresh()
  }
  async deletePerson(id: string): Promise<void> {
    this.conns.get(id)?.offline()
    await directoryApi.deletePerson(id)
    await this.refresh()
  }

  async addMember(personId: string, groupId: string): Promise<void> {
    await directoryApi.addMember(personId, groupId)
    await this.refresh()
    const group = this.groups.find((g) => g.id === groupId)
    if (group) this.conns.get(personId)?.join(group.name)
  }
  async removeMember(personId: string, groupId: string): Promise<void> {
    await directoryApi.removeMember(personId, groupId)
    const group = this.groups.find((g) => g.id === groupId)
    if (group) this.conns.get(personId)?.leave(group.name)
    await this.refresh()
  }

  // ---- live (ephemeral) ----------------------------------------------------

  setToken(v: string): void {
    this.token = v
    saveLS('pubsub.token', v)
    this.commit()
  }
  setEndpoint(v: string): void {
    this.endpoint = v
    saveLS('pubsub.endpoint', v)
    this.commit()
  }

  /** The group topic names a person belongs to (persistent membership → topics). */
  groupsFor(personId: string): string[] {
    const ids = new Set(
      this.memberships.filter((m) => m.person_id === personId).map((m) => m.group_id),
    )
    return this.groups.filter((g) => ids.has(g.id)).map((g) => g.name)
  }

  bringOnline(personId: string): void {
    this.conns.get(personId)?.online(this.groupsFor(personId), this.token, this.endpoint)
  }
  takeOffline(personId: string): void {
    this.conns.get(personId)?.offline()
  }

  // ---- internals -----------------------------------------------------------

  private commit(): void {
    this.snapshot = this.build()
    for (const fn of this.listeners) fn()
  }

  private build(): PeopleSnapshot {
    const status: Record<string, PersonStatus> = {}
    for (const [id, c] of this.conns) status[id] = c.status
    return {
      people: this.people,
      groups: this.groups,
      memberships: this.memberships,
      status,
      token: this.token,
      endpoint: this.endpoint,
    }
  }
}

export const peopleStore = new PeopleStore()
