import { useState } from 'react'
import { Waypoints } from 'lucide-react'
import { Room } from '@/components/Room'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export default function App() {
  const [room, setRoom] = useState('demo')
  const [name, setName] = useState('me')
  const [joined, setJoined] = useState<{ room: string; name: string } | null>(null)

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <header className="flex items-center gap-3">
        <Waypoints className="text-primary size-6" />
        <div>
          <h1 className="text-xl font-semibold">WebRTC SFU</h1>
          <p className="text-muted-foreground text-sm">
            Project 15 · a room client — camera + signaling wired, media path yours to build
          </p>
        </div>
      </header>

      {!joined ? (
        <Card>
          <CardContent className="flex flex-wrap items-end gap-3 pt-6">
            <div className="grid gap-1.5">
              <Label htmlFor="room">Room</Label>
              <Input
                id="room"
                value={room}
                onChange={(e) => setRoom(e.target.value)}
                className="w-44 font-mono"
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="name">Display name</Label>
              <Input id="name" value={name} onChange={(e) => setName(e.target.value)} className="w-44" />
            </div>
            <Button onClick={() => setJoined({ room, name })}>Join room</Button>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">room</span>
            <code className="font-mono">{joined.room}</code>
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto"
              onClick={() => setJoined(null)}
            >
              Leave
            </Button>
          </div>
          <Room room={joined.room} name={joined.name} />
        </>
      )}
    </div>
  )
}
