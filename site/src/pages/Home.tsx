import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { CURRENT_FOCUS, REPO_URL, currentProject } from '@/data/roadmap'
import { ProgressBar } from '@/components/ProgressBar'
import { StateBadge } from '@/components/StateBadge'

export function Home() {
  const focus = currentProject()

  return (
    <div className="space-y-16">
      <section className="relative min-h-[70vh] flex flex-col justify-center gap-8 py-4">
        <div
          className="progress-glow pointer-events-none absolute -left-24 top-8 h-48 w-48 rounded-full bg-copper/20 blur-3xl"
          aria-hidden
        />

        <p className="animate-fade-up font-mono text-[0.7rem] uppercase tracking-[0.2em] text-copper">
          learning lab · infrastructure in Rust
        </p>

        <h1 className="animate-fade-up-delay font-display text-5xl font-extrabold leading-[0.95] tracking-tight text-fg sm:text-6xl md:text-7xl">
          backend-gauntlet
        </h1>

        <p className="animate-fade-up-delay-2 max-w-xl text-lg text-fg-muted sm:text-xl">
          Build the infrastructure primitives that power the modern web — queues,
          caches, brokers, consensus — from scratch. Scaffolded SPECs, interesting
          logic left as <code className="font-mono text-copper">todo!()</code>.
        </p>

        <div className="animate-fade-up-delay-2 flex flex-wrap items-center gap-3 pt-2">
          <Link
            to="/roadmap"
            className="inline-flex items-center gap-2 rounded bg-copper px-4 py-2.5 font-mono text-[0.75rem] uppercase tracking-wider text-bg no-underline transition hover:bg-[#e89455]"
          >
            View roadmap
            <ArrowRight className="size-3.5" />
          </Link>
          <Link
            to="/method"
            className="inline-flex items-center gap-2 rounded border border-line px-4 py-2.5 font-mono text-[0.75rem] uppercase tracking-wider text-fg no-underline transition hover:border-copper/50"
          >
            How I learn
          </Link>
          <a
            href={REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-2 px-2 py-2.5 font-mono text-[0.75rem] uppercase tracking-wider text-fg-muted no-underline hover:text-copper"
          >
            Source
          </a>
        </div>

        <div className="animate-fade-up-delay-2 mt-4 max-w-lg border-t border-line pt-6">
          <p className="mb-2 font-mono text-[0.65rem] uppercase tracking-[0.18em] text-fg-muted">
            Current focus
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <Link
              to={`/projects/${CURRENT_FOCUS}`}
              className="font-display text-xl font-bold text-fg no-underline hover:text-copper"
            >
              {focus.id} · {focus.name}
            </Link>
            <StateBadge state={focus.state} />
          </div>
          <ProgressBar value={focus.progress} className="mt-3" />
          <p className="mt-2 font-mono text-[0.7rem] text-fg-muted">
            {focus.progress}% · {focus.blurb}
          </p>
        </div>
      </section>

      <section className="space-y-4">
        <div className="flex items-end justify-between gap-4">
          <h2 className="font-display text-2xl font-bold tracking-tight">
            Progress
          </h2>
          <p className="font-mono text-[0.65rem] uppercase tracking-wider text-fg-muted">
            from make status
          </p>
        </div>
        <div className="overflow-hidden rounded-lg border border-line bg-bg-elevated/80 p-2 sm:p-3">
          <img
            src={`${import.meta.env.BASE_URL}status-dashboard.svg`}
            alt="backend-gauntlet progress dashboard across all projects"
            className="w-full"
          />
        </div>
      </section>
    </div>
  )
}
