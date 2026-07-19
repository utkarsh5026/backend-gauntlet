import { ExternalLink } from 'lucide-react'
import { project01 } from '@/data/project01'
import { StateBadge } from '@/components/StateBadge'

export function Project01() {
  return (
    <article className="space-y-14 animate-fade-up">
      <header className="max-w-2xl space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <p className="font-mono text-[0.7rem] uppercase tracking-[0.2em] text-copper">
            Project {project01.id}
          </p>
          <StateBadge state="active" />
        </div>
        <h1 className="font-display text-4xl font-extrabold tracking-tight sm:text-5xl">
          {project01.title}
        </h1>
        <p className="text-lg text-fg-muted">{project01.tagline}</p>
        <div className="flex flex-wrap gap-3 pt-2">
          <a
            href={project01.links.spec}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 font-mono text-[0.75rem] uppercase tracking-wider"
          >
            SPEC.md <ExternalLink className="size-3" />
          </a>
          <a
            href={project01.links.code}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 font-mono text-[0.75rem] uppercase tracking-wider text-fg-muted"
          >
            Source <ExternalLink className="size-3" />
          </a>
        </div>
      </header>

      <section className="max-w-2xl space-y-3">
        <h2 className="font-display text-2xl font-bold">Problem</h2>
        <p className="text-fg-muted">{project01.problem}</p>
        <ul className="mt-4 list-disc space-y-1.5 pl-5 text-fg-muted">
          {project01.whatItDoes.map((line) => (
            <li key={line}>
              <code className="font-mono text-[0.9em] text-fg">{line}</code>
            </li>
          ))}
        </ul>
      </section>

      <section className="space-y-4">
        <h2 className="font-display text-2xl font-bold">Mental model</h2>
        <p className="max-w-2xl text-fg-muted">
          Request path at a glance — create feeds the hot redirect path; cache and
          async ingest keep Postgres off the critical path.
        </p>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-stretch sm:gap-0">
          {project01.flow.map((step, i) => (
            <div key={step.id} className="flex flex-1 items-stretch sm:contents">
              <div className="flex-1 rounded-lg border border-line bg-bg-elevated/60 px-4 py-4 sm:rounded-none sm:first:rounded-l-lg sm:last:rounded-r-lg">
                <p className="font-mono text-[0.65rem] uppercase tracking-wider text-copper">
                  {step.label}
                </p>
                <p className="mt-1 text-sm text-fg-muted">{step.detail}</p>
              </div>
              {i < project01.flow.length - 1 && (
                <div
                  className="flex items-center justify-center px-1 font-mono text-copper/60 sm:px-2"
                  aria-hidden
                >
                  →
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      <section className="space-y-6">
        <h2 className="font-display text-2xl font-bold">Verticals</h2>
        <p className="max-w-2xl text-fg-muted">
          Concepts to internalize — not solution walkthroughs. The SPEC owns the
          acceptance criteria.
        </p>
        <ul className="space-y-5">
          {project01.verticals.map((v) => (
            <li
              key={v.id}
              className="grid gap-1 border-l-2 border-copper/40 pl-4 sm:grid-cols-[4rem_1fr]"
            >
              <span className="font-mono text-sm text-copper">{v.id}</span>
              <div>
                <h3 className="font-display text-lg font-bold">{v.title}</h3>
                <p className="mt-1 text-fg-muted">{v.concept}</p>
                <p className="mt-2 font-mono text-[0.7rem] text-fg-muted/80">
                  {v.module}
                </p>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="max-w-2xl space-y-3">
        <h2 className="font-display text-2xl font-bold">Horizontals</h2>
        <ul className="list-disc space-y-2 pl-5 text-fg-muted">
          {project01.horizontals.map((h) => (
            <li key={h}>{h}</li>
          ))}
        </ul>
      </section>

      <section className="max-w-2xl space-y-3 rounded-lg border border-copper/25 bg-copper/5 px-5 py-6">
        <p className="font-mono text-[0.7rem] uppercase tracking-[0.18em] text-copper">
          Boss fight
        </p>
        <h2 className="font-display text-2xl font-bold">{project01.boss.name}</h2>
        <p className="text-fg-muted">{project01.boss.idea}</p>
      </section>
    </article>
  )
}
