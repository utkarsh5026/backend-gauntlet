// The admin panel: create people & groups (each with a Notion-style emoji
// avatar), flip a person online/offline, and assign them to groups — all
// persisted in Postgres via the directory API.
//
// Mental model: the roster (people/groups/memberships) is durable "hard state";
// online/offline is live state — bringing a person online opens a real
// WebSocket under their identity and subscribes it to their groups (people-store).

import { useEffect, useState } from 'react'
import { Plus, Trash2, UserPlus, Users, X } from 'lucide-react'

import { usePeople } from '@/hooks/usePeople'
import type { Group, Person } from '@/lib/directory'
import { peopleStore, type PersonStatus } from '@/lib/people-store'
import { cn } from '@/lib/utils'

import { Avatar } from './Avatar'
import { EmojiPicker, type EmojiValue } from './EmojiPicker'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { Input } from './ui/input'

const DEFAULT_PERSON: EmojiValue = { emoji: '🧘', color: '#6366f1' }
const DEFAULT_GROUP: EmojiValue = { emoji: '🎨', color: '#10b981' }

function statusLabel(s: PersonStatus): string {
  switch (s) {
    case 'online':
      return 'online'
    case 'connecting':
      return 'connecting…'
    case 'error':
      return 'connection failed'
    default:
      return 'offline'
  }
}

/** A minimal on/off switch (no extra dependency). */
function Toggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={cn(
        'relative h-5 w-9 shrink-0 rounded-full transition-colors',
        on ? 'bg-green-500' : 'bg-muted',
      )}
    >
      <span
        className={cn(
          'absolute top-0.5 size-4 rounded-full bg-white shadow transition-all',
          on ? 'left-[18px]' : 'left-0.5',
        )}
      />
    </button>
  )
}

/** Name + emoji/color picker + create button, shared by people and groups. */
function CreateForm({
  placeholder,
  defaults,
  onCreate,
}: {
  placeholder: string
  defaults: EmojiValue
  onCreate: (name: string, icon: EmojiValue) => Promise<void>
}) {
  const [name, setName] = useState('')
  const [icon, setIcon] = useState<EmojiValue>(defaults)
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    const n = name.trim()
    if (!n || busy) return
    setBusy(true)
    try {
      await onCreate(n, icon)
      setName('')
      setIcon(defaults)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex items-center gap-2">
      <EmojiPicker value={icon} onChange={setIcon} size={40} />
      <Input
        value={name}
        placeholder={placeholder}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void submit()
        }}
      />
      <Button size="sm" onClick={() => void submit()} disabled={busy || !name.trim()}>
        <Plus /> Add
      </Button>
    </div>
  )
}

/** "+ group" button revealing the groups a person isn't in yet. */
function AddToGroupMenu({
  available,
  onPick,
}: {
  available: Group[]
  onPick: (groupId: string) => void
}) {
  const [open, setOpen] = useState(false)
  if (available.length === 0) return null
  return (
    <div className="relative inline-block">
      <Button variant="outline" size="sm" className="h-6 px-2 text-xs" onClick={() => setOpen((v) => !v)}>
        <Plus className="size-3" /> group
      </Button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="bg-popover absolute left-0 z-50 mt-1 max-h-48 w-44 overflow-y-auto rounded-md border p-1 shadow-lg">
            {available.map((g) => (
              <button
                key={g.id}
                type="button"
                onClick={() => {
                  onPick(g.id)
                  setOpen(false)
                }}
                className="hover:bg-accent flex w-full items-center gap-2 rounded px-2 py-1 text-left text-sm"
              >
                <Avatar emoji={g.emoji} color={g.color} size={18} />
                <span className="truncate">{g.name}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function PersonRow({
  person,
  status,
  inGroups,
  available,
}: {
  person: Person
  status: PersonStatus
  inGroups: Group[]
  available: Group[]
}) {
  const online = status === 'online' || status === 'connecting'
  return (
    <div className="flex items-start gap-3 rounded-lg border p-3">
      <Avatar emoji={person.emoji} color={person.color} size={40} online={status === 'online'} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate font-medium">{person.name}</span>
          <span
            className={cn(
              'text-xs',
              status === 'online'
                ? 'text-green-500'
                : status === 'error'
                  ? 'text-destructive'
                  : 'text-muted-foreground',
            )}
          >
            {statusLabel(status)}
          </span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {inGroups.map((g) => (
            <Badge key={g.id} variant="secondary" className="gap-1 pl-1">
              <Avatar emoji={g.emoji} color={g.color} size={14} />
              {g.name}
              <button
                type="button"
                aria-label={`Remove from ${g.name}`}
                onClick={() => void peopleStore.removeMember(person.id, g.id)}
                className="hover:text-destructive ml-0.5"
              >
                <X className="size-3" />
              </button>
            </Badge>
          ))}
          <AddToGroupMenu
            available={available}
            onPick={(gid) => void peopleStore.addMember(person.id, gid)}
          />
        </div>
      </div>
      <div className="flex flex-col items-end gap-2">
        <Toggle
          on={online}
          onClick={() => (online ? peopleStore.takeOffline(person.id) : peopleStore.bringOnline(person.id))}
        />
        <button
          type="button"
          aria-label={`Delete ${person.name}`}
          onClick={() => void peopleStore.deletePerson(person.id)}
          className="text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="size-4" />
        </button>
      </div>
    </div>
  )
}

function GroupRow({ group, members }: { group: Group; members: Person[] }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border p-3">
      <Avatar emoji={group.emoji} color={group.color} size={36} />
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium">{group.name}</div>
        <div className="text-muted-foreground text-xs">
          {members.length} member{members.length === 1 ? '' : 's'}
        </div>
      </div>
      <div className="flex -space-x-2">
        {members.slice(0, 6).map((p) => (
          <Avatar key={p.id} emoji={p.emoji} color={p.color} size={22} className="ring-background ring-2" />
        ))}
      </div>
    </div>
  )
}

function Empty({ label }: { label: string }) {
  return (
    <div className="text-muted-foreground rounded-lg border border-dashed p-6 text-center text-sm">
      {label}
    </div>
  )
}

export function AdminPanel({ onClose }: { onClose: () => void }) {
  const snap = usePeople()
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    peopleStore
      .refresh()
      .then(() => alive && setError(null))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  const groupsOf = (personId: string): Group[] => {
    const ids = new Set(
      snap.memberships.filter((m) => m.person_id === personId).map((m) => m.group_id),
    )
    return snap.groups.filter((g) => ids.has(g.id))
  }
  const availableFor = (personId: string): Group[] => {
    const ids = new Set(
      snap.memberships.filter((m) => m.person_id === personId).map((m) => m.group_id),
    )
    return snap.groups.filter((g) => !ids.has(g.id))
  }
  const membersOf = (groupId: string): Person[] => {
    const ids = new Set(
      snap.memberships.filter((m) => m.group_id === groupId).map((m) => m.person_id),
    )
    return snap.people.filter((p) => ids.has(p.id))
  }

  return (
    <div className="bg-background fixed inset-0 z-50 flex flex-col">
      <header className="flex shrink-0 items-center gap-3 border-b px-4 py-2.5">
        <div className="bg-primary/10 text-primary flex size-8 items-center justify-center rounded-lg">
          <Users className="size-4" />
        </div>
        <div className="min-w-0">
          <h2 className="text-sm font-semibold leading-tight">Directory</h2>
          <p className="text-muted-foreground text-xs leading-tight">People, groups & who's online</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Input
            value={snap.token}
            placeholder="ws token"
            onChange={(e) => peopleStore.setToken(e.target.value)}
            className="h-8 w-40"
          />
          <Button size="sm" variant="ghost" onClick={onClose}>
            <X /> Close
          </Button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-6">
        {error ? (
          <div className="border-destructive/40 bg-destructive/10 text-destructive mx-auto max-w-2xl rounded-lg border p-4 text-sm">
            <p className="font-medium">Roster unavailable</p>
            <p className="mt-1 opacity-90">{error}</p>
            <p className="text-muted-foreground mt-2 text-xs">
              Enable it: uncomment <code>DATABASE_URL</code> in <code>.env</code>, run{' '}
              <code>docker compose up -d postgres</code>, then <code>make prepare</code>.
            </p>
          </div>
        ) : (
          <div className="mx-auto max-w-5xl space-y-6">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="rounded-xl border p-4">
                <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                  <UserPlus className="size-4" /> New person
                </div>
                <CreateForm
                  placeholder="Name, e.g. Alice"
                  defaults={DEFAULT_PERSON}
                  onCreate={(n, v) => peopleStore.createPerson(n, v.emoji, v.color)}
                />
              </div>
              <div className="rounded-xl border p-4">
                <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                  <Plus className="size-4" /> New group
                </div>
                <CreateForm
                  placeholder="Topic, e.g. eng"
                  defaults={DEFAULT_GROUP}
                  onCreate={(n, v) => peopleStore.createGroup(n, v.emoji, v.color)}
                />
              </div>
            </div>

            <div className="grid gap-6 lg:grid-cols-[1.6fr_1fr]">
              <section>
                <h3 className="text-muted-foreground mb-2 text-xs font-medium uppercase tracking-wide">
                  People · {snap.people.length}
                </h3>
                <div className="space-y-2">
                  {snap.people.map((p) => (
                    <PersonRow
                      key={p.id}
                      person={p}
                      status={snap.status[p.id] ?? 'offline'}
                      inGroups={groupsOf(p.id)}
                      available={availableFor(p.id)}
                    />
                  ))}
                  {!loading && snap.people.length === 0 && <Empty label="No people yet — add one above." />}
                </div>
              </section>

              <section>
                <h3 className="text-muted-foreground mb-2 text-xs font-medium uppercase tracking-wide">
                  Groups · {snap.groups.length}
                </h3>
                <div className="space-y-2">
                  {snap.groups.map((g) => (
                    <GroupRow key={g.id} group={g} members={membersOf(g.id)} />
                  ))}
                  {!loading && snap.groups.length === 0 && <Empty label="No groups yet — add one above." />}
                </div>
              </section>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
