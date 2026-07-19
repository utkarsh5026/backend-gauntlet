import { Link } from 'react-router-dom'
import { tiers } from '@/data/roadmap'
import { ProgressBar } from '@/components/ProgressBar'
import { StateBadge } from '@/components/StateBadge'

export function Roadmap() {
  return (
    <div className="space-y-12 animate-fade-up">
      <header className="max-w-2xl space-y-4">
        <p className="font-mono text-[0.7rem] uppercase tracking-[0.2em] text-copper">
          22 projects · 7 tiers
        </p>
        <h1 className="font-display text-4xl font-extrabold tracking-tight sm:text-5xl">
          Roadmap
        </h1>
        <p className="text-lg text-fg-muted">
          Easy → hard. Verticals build the core; horizontals weave in production
          habits. Progress percentages are snapshots from{' '}
          <code className="font-mono text-copper">make status</code>.
        </p>
      </header>

      <div className="space-y-14">
        {tiers.map((tier) => (
          <section key={tier.id} className="space-y-4">
            <div className="border-b border-line pb-3">
              <h2 className="font-display text-xl font-bold">{tier.label}</h2>
              <p className="font-mono text-[0.7rem] text-fg-muted">{tier.theme}</p>
            </div>
            <ul className="space-y-3">
              {tier.projects.map((p) => {
                const inner = (
                  <>
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-2.5">
                        <span className="font-mono text-sm text-copper">
                          {p.id}
                        </span>
                        <span className="font-display font-bold text-fg">
                          {p.name}
                        </span>
                        <StateBadge state={p.state} />
                      </div>
                      <span className="font-mono text-[0.7rem] text-fg-muted">
                        {p.progress}%
                      </span>
                    </div>
                    <p className="mt-1 text-sm text-fg-muted">{p.blurb}</p>
                    <ProgressBar value={p.progress} className="mt-3" />
                  </>
                )

                return (
                  <li key={p.id}>
                    {p.href ? (
                      <Link
                        to={p.href}
                        className="block rounded-lg border border-line bg-bg-elevated/50 px-4 py-3.5 no-underline transition hover:border-copper/40"
                      >
                        {inner}
                      </Link>
                    ) : (
                      <div className="rounded-lg border border-line/70 px-4 py-3.5 opacity-90">
                        {inner}
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          </section>
        ))}
      </div>
    </div>
  )
}
