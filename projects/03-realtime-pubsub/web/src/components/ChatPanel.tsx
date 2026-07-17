import { useEffect, useRef } from 'react'
import { Hash, MessageSquareDashed, Users } from 'lucide-react'

import { Composer } from '@/components/Composer'
import { MessageBubble } from '@/components/MessageBubble'
import type { RoomSnapshot } from '@/lib/store'

export function ChatPanel({ room }: { room: RoomSnapshot | null }) {
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [room?.messages.length])

  if (!room) {
    return (
      <div className="text-muted-foreground flex flex-1 flex-col items-center justify-center gap-2 text-sm">
        <MessageSquareDashed className="size-8" />
        Join a room from the sidebar to start chatting.
      </div>
    )
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <header className="flex items-center gap-2 border-b px-4 py-3">
        <Hash className="text-muted-foreground size-4" />
        <h2 className="truncate font-mono text-sm font-semibold">{room.topic}</h2>
        <div className="text-muted-foreground ml-auto flex min-w-0 items-center gap-1.5 text-xs">
          <Users className="size-3.5 shrink-0" />
          {room.members.length === 0 ? (
            <span>nobody else yet</span>
          ) : (
            <span className="truncate">{room.members.join(', ')}</span>
          )}
        </div>
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-2">
        {room.messages.length === 0 ? (
          <p className="text-muted-foreground/60 py-10 text-center text-sm">No messages yet — say something.</p>
        ) : (
          room.messages.map((e, i) => {
            const prev = room.messages[i - 1]
            const showHeader = e.kind !== 'message' || !prev || prev.kind !== 'message' || prev.from !== e.from
            return <MessageBubble key={e.id} e={e} showHeader={showHeader} />
          })
        )}
      </div>

      <Composer topic={room.topic} />
    </div>
  )
}
