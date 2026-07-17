import { useState } from 'react'
import { Hash, LogOut, Plug, Plus, Power, Settings2 } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { store } from '@/lib/store'
import { cn } from '@/lib/utils'
import type { Snapshot, Status } from '@/lib/store'

const statusMeta: Record<Status, { dot: string; label: string }> = {
  open: { dot: 'bg-success', label: 'connected' },
  connecting: { dot: 'bg-amber-400 animate-pulse', label: 'connecting' },
  closed: { dot: 'bg-muted-foreground/50', label: 'offline' },
  error: { dot: 'bg-destructive', label: 'error' },
}

export function Sidebar({ snap }: { snap: Snapshot }) {
  const [showSettings, setShowSettings] = useState(false)
  const [joinInput, setJoinInput] = useState('')
  const meta = statusMeta[snap.status]
  const connected = snap.status === 'open'

  const join = () => {
    if (joinInput.trim()) {
      store.joinRoom(joinInput)
      setJoinInput('')
    }
  }

  return (
    <aside className="bg-card flex w-72 shrink-0 flex-col border-r">
      {/* identity */}
      <div className="flex flex-col gap-2 border-b p-3">
        <div className="flex items-center gap-2">
          <span className={cn('size-2.5 shrink-0 rounded-full', meta.dot)} title={meta.label} />
          <input
            value={snap.name}
            onChange={(e) => store.setName(e.target.value)}
            spellCheck={false}
            className="min-w-0 flex-1 bg-transparent text-sm font-medium outline-none focus:underline"
          />
          <Button
            size="icon"
            variant="ghost"
            className="size-7"
            title="connection settings"
            onClick={() => setShowSettings((v) => !v)}
          >
            <Settings2 className="size-3.5" />
          </Button>
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-muted-foreground text-xs">{meta.label}</span>
          {connected ? (
            <Button size="sm" variant="outline" className="h-7" onClick={() => store.disconnect()}>
              <Power className="size-3.5" /> Disconnect
            </Button>
          ) : (
            <Button size="sm" className="h-7" onClick={() => store.connect()}>
              <Plug className="size-3.5" /> Connect
            </Button>
          )}
        </div>
        {snap.error && <p className="text-destructive text-xs">{snap.error}</p>}

        {showSettings && (
          <div className="flex flex-col gap-2 pt-1">
            <div className="flex flex-col gap-1">
              <Label className="text-[11px]">Endpoint</Label>
              <Input
                value={snap.endpoint}
                onChange={(e) => store.setEndpoint(e.target.value)}
                placeholder="/ws"
                className="h-7 font-mono text-xs"
                spellCheck={false}
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label className="text-[11px]">
                Token <span className="text-muted-foreground font-normal">(→ ?token=)</span>
              </Label>
              <Input
                value={snap.token}
                onChange={(e) => store.setToken(e.target.value)}
                placeholder="optional"
                className="h-7 font-mono text-xs"
                spellCheck={false}
              />
            </div>
          </div>
        )}
      </div>

      {/* rooms */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <div className="text-muted-foreground px-3 pt-3 pb-1 text-xs font-medium tracking-wide uppercase">Rooms</div>
        <div className="flex-1 overflow-y-auto px-2">
          {snap.rooms.length === 0 ? (
            <p className="text-muted-foreground/70 px-2 py-4 text-xs">Join a room below to start chatting.</p>
          ) : (
            <ul className="flex flex-col gap-0.5 pb-2">
              {snap.rooms.map((room) => {
                const active = room.topic === snap.activeTopic
                return (
                  <li
                    key={room.topic}
                    className={cn('group flex items-center gap-0.5 rounded-md', active ? 'bg-accent' : 'hover:bg-accent/50')}
                  >
                    <button
                      onClick={() => store.setActiveTopic(room.topic)}
                      className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 pl-2 text-left text-sm"
                    >
                      <Hash className="text-muted-foreground size-3.5 shrink-0" />
                      <span className={cn('min-w-0 flex-1 truncate font-mono', active && 'text-accent-foreground font-medium')}>
                        {room.topic}
                      </span>
                      {room.unread > 0 && !active && (
                        <Badge className="h-4 min-w-4 shrink-0 justify-center rounded-full px-1 text-[10px]">
                          {room.unread}
                        </Badge>
                      )}
                      {room.members.length > 0 && (
                        <span className="text-muted-foreground shrink-0 text-[10px] tabular-nums">{room.members.length}</span>
                      )}
                    </button>
                    <button
                      onClick={() => store.leaveRoom(room.topic)}
                      title="leave room"
                      className="text-muted-foreground hover:text-destructive mr-1 hidden shrink-0 rounded p-1 group-hover:block"
                    >
                      <LogOut className="size-3.5" />
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
        <div className="flex items-center gap-1.5 border-t p-2">
          <Input
            value={joinInput}
            onChange={(e) => setJoinInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && join()}
            placeholder="join room…"
            className="h-8 font-mono text-xs"
            spellCheck={false}
          />
          <Button size="icon" variant="secondary" className="size-8 shrink-0" onClick={join} title="join room">
            <Plus className="size-4" />
          </Button>
        </div>
      </div>
    </aside>
  )
}
