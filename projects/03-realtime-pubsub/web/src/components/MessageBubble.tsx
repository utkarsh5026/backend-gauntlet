import { CircleAlert } from 'lucide-react'

import { cn } from '@/lib/utils'
import { fmtLatency, initials, shortTime } from '@/lib/format'
import type { ThreadEntry } from '@/lib/store'

export function MessageBubble({ e, showHeader }: { e: ThreadEntry; showHeader: boolean }) {
  if (e.kind === 'system') {
    return <p className="text-muted-foreground/70 py-1 text-center text-[11px] italic">{e.text}</p>
  }

  if (e.kind === 'error') {
    return (
      <p className="text-destructive flex items-center justify-center gap-1 py-1 text-center text-[11px]">
        <CircleAlert className="size-3" /> {e.text}
      </p>
    )
  }

  const mine = Boolean(e.mine)
  return (
    <div className={cn('flex items-end gap-2', mine ? 'flex-row-reverse' : 'flex-row', showHeader ? 'mt-3' : 'mt-0.5')}>
      {!mine && (
        <span
          className={cn(
            'bg-muted text-muted-foreground flex size-6 shrink-0 items-center justify-center rounded-full text-[10px] font-medium',
            !showHeader && 'invisible',
          )}
        >
          {initials(e.from ?? '?')}
        </span>
      )}
      <div className={cn('flex max-w-[75%] flex-col', mine ? 'items-end' : 'items-start')}>
        {showHeader && !mine && <span className="text-muted-foreground mb-0.5 px-1 text-[11px]">{e.from}</span>}
        <div
          className={cn(
            'rounded-2xl px-3 py-1.5 text-sm break-words',
            mine ? 'bg-primary text-primary-foreground rounded-br-sm' : 'bg-muted text-foreground rounded-bl-sm',
          )}
        >
          {e.text}
        </div>
        <span className="text-muted-foreground/60 mt-0.5 px-1 text-[10px] tabular-nums">
          {shortTime(e.at)}
          {e.latencyMs != null && <> · {fmtLatency(e.latencyMs)}</>}
        </span>
      </div>
    </div>
  )
}
