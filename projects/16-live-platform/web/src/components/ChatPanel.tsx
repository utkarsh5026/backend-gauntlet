import { useEffect, useRef, useState } from 'react'
import { Send } from 'lucide-react'
import { chatSocketUrl, type ChatMessage } from '@/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

type Conn = 'connecting' | 'open' | 'closed'

/**
 * The channel chat + presence panel — project 03's WebSocket fan-out, now
 * multi-tenant. The socket protocol (`chat_socket` in routes.rs) is yours to
 * define; this client assumes JSON `ChatMessage` frames inbound and sends
 * `{ user, body }` outbound. Tweak `onmessage`/`send` to match what you build.
 */
export function ChatPanel({ stream, user }: { stream: string; user: string }) {
  const [conn, setConn] = useState<Conn>('connecting')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const wsRef = useRef<WebSocket | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setMessages([])
    setConn('connecting')
    const ws = new WebSocket(chatSocketUrl(stream))
    wsRef.current = ws
    ws.onopen = () => setConn('open')
    ws.onclose = () => setConn('closed')
    ws.onerror = () => setConn('closed')
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data as string) as ChatMessage
        setMessages((prev) => [...prev.slice(-200), msg])
      } catch {
        // Non-JSON frame (e.g. a presence ping) — ignore until the protocol firms up.
      }
    }
    return () => ws.close()
  }, [stream])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages])

  const send = () => {
    const ws = wsRef.current
    const body = draft.trim()
    if (!ws || ws.readyState !== WebSocket.OPEN || !body) return
    ws.send(JSON.stringify({ user, body }))
    setDraft('')
  }

  return (
    <div className="flex h-full flex-col rounded-xl border">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <span className="text-sm font-semibold">Chat</span>
        <Badge variant={conn === 'open' ? 'default' : conn === 'connecting' ? 'secondary' : 'destructive'}>
          {conn}
        </Badge>
      </div>

      <div ref={scrollRef} className="flex-1 space-y-2 overflow-y-auto p-4">
        {messages.length === 0 && (
          <p className="text-muted-foreground text-xs">
            {conn === 'open' ? 'No messages yet — say hi.' : 'Waiting for the channel socket…'}
          </p>
        )}
        {messages.map((m, i) => (
          <div key={`${m.sent_at_ms}-${i}`} className="text-sm">
            <span className="text-primary font-medium">{m.user || 'anon'}</span>{' '}
            <span className="text-foreground/90">{m.body}</span>
          </div>
        ))}
      </div>

      <div className="flex gap-2 border-t p-3">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder={conn === 'open' ? 'Message…' : 'Disconnected'}
          disabled={conn !== 'open'}
        />
        <Button size="icon" onClick={send} disabled={conn !== 'open'}>
          <Send className="size-4" />
        </Button>
      </div>
    </div>
  )
}
