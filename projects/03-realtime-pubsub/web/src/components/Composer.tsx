import { useState } from 'react'
import { Send } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { store } from '@/lib/store'

export function Composer({ topic }: { topic: string }) {
  const [text, setText] = useState('')

  const send = () => {
    if (!text.trim()) return
    store.sendMessage(topic, text)
    setText('')
  }

  return (
    <div className="flex items-center gap-2 border-t p-3">
      <Input
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && send()}
        placeholder={`Message ${topic}…`}
        className="h-10"
        autoFocus
      />
      <Button size="icon" className="size-10 shrink-0" onClick={send} disabled={!text.trim()} title="send">
        <Send className="size-4" />
      </Button>
    </div>
  )
}
