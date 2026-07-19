import { Link, Navigate, useParams } from 'react-router-dom'
import { ExternalLink } from 'lucide-react'
import { getProject, projectLinks } from '@/data/projects'
import { findProject } from '@/data/roadmap'
import { StateBadge } from '@/components/StateBadge'

export function ProjectDetail() {
  const { id } = useParams<{ id: string }>()
  const detail = id ? getProject(id) : undefined
  const meta = id ? findProject(id) : undefined

  if (!detail) {
    return <Navigate to="/" replace />
  }

  const links = projectLinks(detail)

  return (
    <article className="space-y-14 animate-fade-up">
      <header className="max-w-2xl space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <p className="font-mono text-[0.7rem] uppercase tracking-[0.2em] text-copper">
            Project {detail.id}
          </p>
          {meta && <StateBadge state={meta.state} />}
        </div>
        <h1 className="font-display text-4xl font-extrabold tracking-tight sm:text-5xl">
          {detail.title}
        </h1>
        <p className="text-lg text-fg-muted">{detail.tagline}</p>
        <div className="flex flex-wrap gap-3 pt-2">
          <a
            href={links.spec}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 font-mono text-[0.75rem] uppercase tracking-wider"
          >
            SPEC.md <ExternalLink className="size-3" />
          </a>
          <a
            href={links.code}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 font-mono text-[0.75rem] uppercase tracking-wider text-fg-muted"
          >
            Source <ExternalLink className="size-3" />
          </a>
          <Link
            to="/roadmap"
            className="inline-flex items-center gap-1.5 font-mono text-[0.75rem] uppercase tracking-wider text-fg-muted no-underline hover:text-copper"
          >
            All projects
          </Link>
        </div>
      </header>

      <section className="max-w-2xl space-y-3">
        <h2 className="font-display text-2xl font-bold">Problem</h2>
        <p className="text-fg-muted">{detail.problem}</p>
        {detail.whatItDoes.length > 0 && (
          <ul className="mt-4 list-disc space-y-1.5 pl-5 text-fg-muted">
            {detail.whatItDoes.map((line) => (
              <li key={line}>
                <code className="font-mono text-[0.9em] text-fg">{line}</code>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-4">
        <h2 className="font-display text-2xl font-bold">Mental model</h2>
        <p className="max-w-2xl text-fg-muted">
          Verticals in order — each is a concept to internalize, not a solution
          walkthrough.
        </p>
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-stretch">
          {detail.verticals.map((step, i) => (
            <div key={step.id} className="flex items-stretch sm:contents">
              <div className="min-w-0 flex-1 rounded-lg border border-line bg-bg-elevated/60 px-4 py-4 sm:max-w-[11rem]">
                <p className="font-mono text-[0.65rem] uppercase tracking-wider text-copper">
                  {step.id}
                </p>
                <p className="mt-1 text-sm font-medium text-fg">{step.title}</p>
              </div>
              {i < detail.verticals.length - 1 && (
                <div
                  className="flex items-center justify-center px-1 font-mono text-copper/60 sm:px-1.5"
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
          Concepts to internalize. The SPEC owns the acceptance criteria and
          proofs.
        </p>
        <ul className="space-y-5">
          {detail.verticals.map((v) => (
            <li
              key={v.id}
              className="grid gap-1 border-l-2 border-copper/40 pl-4 sm:grid-cols-[4rem_1fr]"
            >
              <span className="font-mono text-sm text-copper">{v.id}</span>
              <div>
                <h3 className="font-display text-lg font-bold">{v.title}</h3>
                <p className="mt-1 text-fg-muted">{v.concept}</p>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="max-w-2xl space-y-3">
        <h2 className="font-display text-2xl font-bold">Horizontals</h2>
        <ul className="list-disc space-y-2 pl-5 text-fg-muted">
          {detail.horizontals.map((h) => (
            <li key={h}>{h}</li>
          ))}
        </ul>
      </section>

      {detail.boss && (
        <section className="max-w-2xl space-y-3 rounded-lg border border-copper/25 bg-copper/5 px-5 py-6">
          <p className="font-mono text-[0.7rem] uppercase tracking-[0.18em] text-copper">
            Boss fight
          </p>
          <h2 className="font-display text-2xl font-bold">{detail.boss.name}</h2>
          <p className="text-fg-muted">{detail.boss.idea}</p>
        </section>
      )}
    </article>
  )
}
