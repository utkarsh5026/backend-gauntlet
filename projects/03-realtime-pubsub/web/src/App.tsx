import { Radio } from 'lucide-react'

import { ClientGrid } from '@/components/ClientGrid'
import { ConnectionBar } from '@/components/ConnectionBar'
import { LoadPanel } from '@/components/LoadPanel'
import { RoomsPanel } from '@/components/RoomsPanel'
import { StatTiles } from '@/components/StatTiles'
import { usePlayground } from '@/hooks/usePlayground'

export default function App() {
  const snap = usePlayground()

  return (
    <div className="min-h-screen">
      <header className="bg-background/80 sticky top-0 z-10 border-b backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] items-center gap-3 px-6 py-3">
          <div className="bg-primary/10 text-primary flex size-9 items-center justify-center rounded-lg">
            <Radio className="size-5" />
          </div>
          <div className="min-w-0">
            <h1 className="text-sm font-semibold leading-tight">Realtime Pub/Sub — Playground</h1>
            <p className="text-muted-foreground text-xs leading-tight">
              Project 03 · fan-out, presence &amp; backpressure, made visible
            </p>
          </div>
          <a
            href="https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API"
            target="_blank"
            rel="noreferrer"
            className="text-muted-foreground hover:text-foreground ml-auto hidden text-xs sm:block"
          >
            many sockets · one page
          </a>
        </div>
      </header>

      <main className="mx-auto flex max-w-[1600px] flex-col gap-4 px-6 py-6">
        <ConnectionBar snap={snap} />
        <StatTiles totals={snap.totals} />

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[360px_1fr]">
          <aside className="flex flex-col gap-4">
            <RoomsPanel snap={snap} />
            <LoadPanel snap={snap} />
          </aside>
          <section>
            <ClientGrid clients={snap.clients} />
          </section>
        </div>

        <p className="text-muted-foreground/70 pt-2 text-center text-xs">
          Heads up: with the V1 hub's <code className="font-mono">todo!()</code> still in place, the first{' '}
          <span className="font-mono">subscribe</span> or <span className="font-mono">publish</span> panics the server
          and drops your socket — that's the worklist, not a bug. Implement{' '}
          <span className="font-mono">src/hub.rs</span> and it lights up.
        </p>
      </main>
    </div>
  )
}
