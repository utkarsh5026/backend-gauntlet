import { useState } from 'react'
import { MessageCircle, Users, Wrench } from 'lucide-react'

import { AdminPanel } from '@/components/AdminPanel'
import { ChatPanel } from '@/components/ChatPanel'
import { DevPanel } from '@/components/DevPanel'
import { Sidebar } from '@/components/Sidebar'
import { Button } from '@/components/ui/button'
import { useChat } from '@/hooks/useChat'

export default function App() {
  const snap = useChat()
  const [devOpen, setDevOpen] = useState(false)
  const [adminOpen, setAdminOpen] = useState(false)
  const activeRoom = snap.rooms.find((r) => r.topic === snap.activeTopic) ?? null

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <header className="flex shrink-0 items-center gap-3 border-b px-4 py-2.5">
        <div className="bg-primary/10 text-primary flex size-8 items-center justify-center rounded-lg">
          <MessageCircle className="size-4" />
        </div>
        <div className="min-w-0">
          <h1 className="text-sm font-semibold leading-tight">Pub/Sub Chat</h1>
          <p className="text-muted-foreground text-xs leading-tight">Project 03 — a chat room is a topic</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            variant={adminOpen ? 'secondary' : 'ghost'}
            onClick={() => setAdminOpen((v) => !v)}
          >
            <Users className="size-3.5" /> People
          </Button>
          <Button
            size="sm"
            variant={devOpen ? 'secondary' : 'ghost'}
            onClick={() => setDevOpen((v) => !v)}
          >
            <Wrench className="size-3.5" /> Dev tools
          </Button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <Sidebar snap={snap} />
        <ChatPanel room={activeRoom} />
        {devOpen && <DevPanel snap={snap} />}
      </div>

      {adminOpen && <AdminPanel onClose={() => setAdminOpen(false)} />}
    </div>
  )
}
