import { Plug, PlugZap, Power, UserPlus } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { store } from '@/lib/store'
import type { Snapshot } from '@/lib/store'

export function ConnectionBar({ snap }: { snap: Snapshot }) {
  return (
    <div className="bg-card flex flex-col gap-3 rounded-xl border p-4 lg:flex-row lg:items-end">
      <div className="flex flex-1 flex-col gap-1.5">
        <Label htmlFor="endpoint" className="text-xs">
          WebSocket endpoint
        </Label>
        <Input
          id="endpoint"
          value={snap.endpoint}
          onChange={(e) => store.setEndpoint(e.target.value)}
          placeholder="/ws"
          className="font-mono text-sm"
          spellCheck={false}
        />
      </div>
      <div className="flex w-full flex-col gap-1.5 lg:w-64">
        <Label htmlFor="token" className="text-xs">
          Auth token <span className="text-muted-foreground font-normal">(→ ?token=)</span>
        </Label>
        <Input
          id="token"
          value={snap.token}
          onChange={(e) => store.setToken(e.target.value)}
          placeholder="optional"
          className="font-mono text-sm"
          spellCheck={false}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="default" onClick={() => store.connectAll()}>
          <PlugZap /> Connect all
        </Button>
        <Button variant="outline" onClick={() => store.disconnectAll()}>
          <Power /> Disconnect
        </Button>
        <Button variant="secondary" onClick={() => store.addClient()}>
          <UserPlus /> Add
        </Button>
        <Button variant="secondary" onClick={() => store.spawn(4)}>
          <Plug /> Spawn ×4
        </Button>
      </div>
    </div>
  )
}
