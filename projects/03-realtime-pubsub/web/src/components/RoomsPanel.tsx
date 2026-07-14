import { useState } from 'react'
import { Radio, Send, Users } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { store } from '@/lib/store'
import { fmtNum } from '@/lib/format'
import type { Snapshot } from '@/lib/store'

export function RoomsPanel({ snap }: { snap: Snapshot }) {
  const [topic, setTopic] = useState('room1')
  const firstConnected = snap.clients.find((c) => c.status === 'open')?.id ?? null

  const subscribeAll = () => {
    if (topic.trim()) store.subscribeAll(topic)
  }
  const ping = (t: string) => {
    const sender = snap.clients.find((c) => c.status === 'open' && c.subscriptions.includes(t))?.id ?? firstConnected
    if (sender) store.publish(sender, t, { ping: Date.now() })
  }

  return (
    <Card className="gap-4">
      <CardHeader className="[.border-b]:pb-4 border-b">
        <CardTitle className="flex items-center gap-2">
          <Radio className="size-4" /> Rooms
          <Badge variant="secondary" className="ml-auto font-mono">
            {snap.rooms.length}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex gap-2">
          <Input
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && subscribeAll()}
            placeholder="topic"
            className="h-8 font-mono text-sm"
            spellCheck={false}
          />
          <Button size="sm" variant="secondary" className="shrink-0" onClick={subscribeAll} disabled={!firstConnected}>
            Sub all
          </Button>
        </div>

        {snap.rooms.length === 0 ? (
          <p className="text-muted-foreground py-6 text-center text-sm">
            No rooms yet. Connect clients and subscribe them to a topic.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {snap.rooms.map((room) => (
              <li key={room.topic} className="bg-muted/40 rounded-lg border p-3">
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-sm font-medium">{room.topic}</span>
                  <Badge variant="outline" className="ml-auto gap-1 font-mono">
                    <Users className="size-3" />
                    {room.subscriberIds.length}
                  </Badge>
                  <Button
                    size="icon"
                    variant="ghost"
                    className="size-7"
                    title="publish one message to this room"
                    onClick={() => ping(room.topic)}
                    disabled={!firstConnected}
                  >
                    <Send className="size-3.5" />
                  </Button>
                </div>
                <div className="text-muted-foreground mt-1.5 flex items-center gap-3 text-xs">
                  <span>{fmtNum(room.delivered)} delivered</span>
                  <span className="text-border">·</span>
                  {room.members.length > 0 ? (
                    <span className="flex flex-wrap gap-1">
                      {room.members.map((m) => (
                        <span key={m} className="bg-background rounded border px-1.5 py-0.5 font-mono">
                          {m}
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span className="italic">presence lights up once V3 is wired</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
