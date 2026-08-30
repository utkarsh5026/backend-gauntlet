import { AlertTriangle, Check, FileCode2 } from 'lucide-react'

import { STAGES, type StageId, type Status } from '@/lib/stages'

interface Props {
  n: number
  id: StageId
  title: string
  status: Status
  children: React.ReactNode
}

/** One numbered step of the index → refresh → search loop.
 *
 *  Each step states, in order: what number it is, what it does in one plain
 *  sentence, and whether it works yet. The old console scattered these across a
 *  sidebar, a footnote paragraph and a red HTTP error; putting them in one card
 *  is most of the readability fix.
 */
export function Stage({ n, id, title, status, children }: Props) {
  const info = STAGES[id]

  return (
    <section className="bg-card overflow-hidden rounded-xl border">
      <header className="flex flex-wrap items-center gap-3 border-b px-5 py-3.5">
        <span className="bg-muted text-muted-foreground flex size-6 shrink-0 items-center justify-center rounded-full font-mono text-xs font-semibold">
          {n}
        </span>
        <h2 className="text-sm font-semibold">{title}</h2>
        <StatusPill status={status} vertical={info.vertical} />
      </header>

      {/* A container for the step's own controls: they should lay themselves out
          against the width of THIS card, which the drawer can shrink, not against
          the window's. */}
      <div className="@container px-5 py-4">
        <p className="text-muted-foreground mb-4 text-sm leading-relaxed">{info.does}</p>
        {children}
        {status === 'notBuilt' && <NotBuiltNote id={id} />}
      </div>
    </section>
  )
}

function StatusPill({ status, vertical }: { status: Status; vertical: string | null }) {
  if (status === 'works') {
    return (
      <span className="border-success/40 bg-success/10 text-success ml-auto flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs">
        <Check className="size-3" /> working
      </span>
    )
  }
  if (status === 'notBuilt') {
    return (
      <span className="border-warning/40 bg-warning/10 text-warning ml-auto flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs">
        <AlertTriangle className="size-3" /> not built yet{vertical ? ` · ${vertical}` : ''}
      </span>
    )
  }
  if (status === 'broken') {
    return (
      <span className="border-destructive/40 bg-destructive/10 text-destructive ml-auto rounded-full border px-2.5 py-0.5 text-xs">
        error
      </span>
    )
  }
  return (
    <span className="text-muted-foreground ml-auto rounded-full border px-2.5 py-0.5 text-xs">
      untested
    </span>
  )
}

/** The amber "this is your homework" note. Deliberately not red: on a fresh
 *  scaffold this is the expected state, and it names the file to open. */
function NotBuiltNote({ id }: { id: StageId }) {
  const info = STAGES[id]
  return (
    <div className="border-warning/30 bg-warning/5 mt-4 rounded-lg border px-4 py-3">
      <p className="text-warning flex items-center gap-2 text-sm font-medium">
        <AlertTriangle className="size-4 shrink-0" />
        You haven&apos;t written this part yet
      </p>
      <p className="text-muted-foreground mt-1.5 text-xs leading-relaxed">
        The engine raised <code className="text-foreground font-mono">NotImplementedError</code>.
        That is the scaffold working as intended, not a bug.
      </p>
      <p className="text-muted-foreground mt-2 flex items-center gap-1.5 font-mono text-xs">
        <FileCode2 className="size-3.5 shrink-0" />
        <span className="text-foreground">{info.file}</span>
        {info.vertical && <span>({info.vertical})</span>}
      </p>
    </div>
  )
}
