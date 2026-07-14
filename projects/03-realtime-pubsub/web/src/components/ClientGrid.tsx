import { UserPlus } from 'lucide-react'

import { ClientCard } from '@/components/ClientCard'
import { store } from '@/lib/store'
import type { ClientSnapshot } from '@/lib/store'

export function ClientGrid({ clients }: { clients: ClientSnapshot[] }) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-3">
      {clients.map((c) => (
        <ClientCard key={c.id} c={c} />
      ))}
      <button
        onClick={() => store.addClient()}
        className="text-muted-foreground hover:border-ring hover:text-foreground flex min-h-40 items-center justify-center gap-2 rounded-xl border border-dashed text-sm transition-colors"
      >
        <UserPlus className="size-4" /> Add client
      </button>
    </div>
  )
}
