// Thin REST client for the admin-panel roster (Rust `/admin/*` routes, backed by
// directory.rs). This is the PERSISTENT half of the model: people and groups
// that exist whether or not anyone is connected. The LIVE half — actually
// opening sockets and subscribing them — lives in people-store.ts.
//
// Types mirror the Rust `Person` / `Group` / `Membership` structs. Keep them in
// lockstep with projects/03-realtime-pubsub/src/directory.rs.

export interface Person {
  id: string
  name: string
  /** Avatar emoji (e.g. "🧘"), rendered on `color`. */
  emoji: string
  /** Avatar background color (hex). */
  color: string
  autoconnect: boolean
  created_at: string
}

export interface Group {
  id: string
  name: string
  /** Avatar emoji (e.g. "🎨"), rendered on `color`. */
  emoji: string
  color: string
  created_at: string
}

export interface Membership {
  person_id: string
  group_id: string
}

/** Parse a fetch Response, throwing a useful error on non-2xx. 204 → undefined. */
async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${body ? ` — ${body}` : ''}`)
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
}

const jsonHeaders = { 'content-type': 'application/json' }

export const directoryApi = {
  listPeople: () => fetch('/admin/people').then((r) => readJson<Person[]>(r)),

  createPerson: (name: string, emoji: string, color: string) =>
    fetch('/admin/people', {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify({ name, emoji, color }),
    }).then((r) => readJson<Person>(r)),

  deletePerson: (id: string) =>
    fetch(`/admin/people/${id}`, { method: 'DELETE' }).then((r) => readJson<void>(r)),

  listGroups: () => fetch('/admin/groups').then((r) => readJson<Group[]>(r)),

  createGroup: (name: string, emoji: string, color: string) =>
    fetch('/admin/groups', {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify({ name, emoji, color }),
    }).then((r) => readJson<Group>(r)),

  listMemberships: () => fetch('/admin/memberships').then((r) => readJson<Membership[]>(r)),

  addMember: (personId: string, groupId: string) =>
    fetch(`/admin/people/${personId}/groups/${groupId}`, { method: 'POST' }).then((r) =>
      readJson<void>(r),
    ),

  removeMember: (personId: string, groupId: string) =>
    fetch(`/admin/people/${personId}/groups/${groupId}`, { method: 'DELETE' }).then((r) =>
      readJson<void>(r),
    ),
}
