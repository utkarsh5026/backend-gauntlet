import type { ProjectState } from '@/data/roadmap'
import { cn } from '@/lib/utils'

const styles: Record<ProjectState, string> = {
  active: 'bg-copper/15 text-copper border-copper/30',
  paused: 'bg-paused/15 text-paused border-paused/30',
  blocked: 'bg-warn/15 text-warn border-warn/30',
  done: 'bg-ok/15 text-ok border-ok/30',
  'not-started': 'bg-fg-muted/10 text-fg-muted border-line',
}

export function StateBadge({ state }: { state: ProjectState }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[0.65rem] uppercase tracking-wider',
        styles[state],
      )}
    >
      {state}
    </span>
  )
}
