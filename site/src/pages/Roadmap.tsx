import { Link } from 'react-router-dom'
import { tiers } from '@/data/roadmap'
import { ProgressBar } from '@/components/ProgressBar'
import { StateBadge } from '@/components/StateBadge'

export function Roadmap() {
  return (
    <div className="space-y-12">
      <header className="max-w-2xl space-y-3">
        <p className="m-0 text-[0.85rem] text-fg-muted">
          <span className="text-accent">$</span> ls projects/ · 22 projects, 7
          tiers, easy → hard
        </p>
        <h1 className="font-display m-0 text-xl font-bold tracking-tight sm:text-2xl">
          projects
        </h1>
        <p className="m-0 text-fg-muted">
          Click any project for the problem, verticals, and SPEC links. Progress
          is a snapshot from <code className="text-fg">make status</code>.
        </p>
      </header>

      <div className="space-y-10">
        {tiers.map((tier) => (
          <section key={tier.id}>
            <h2 className="rule-title m-0 text-[0.95rem] font-bold">
              {tier.label.toLowerCase()}
              <span className="font-normal text-fg-muted">{tier.theme}</span>
            </h2>
            <ul className="m-0 mt-5 list-none divide-y divide-line border border-line p-0">
              {tier.projects.map((p) => (
                <li key={p.id}>
                  <Link
                    to={p.href ?? `/projects/${p.id}`}
                    className="block px-5 py-5 no-underline transition-colors hover:bg-panel"
                  >
                    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                      <span className="text-[0.85rem] text-fg-muted">{p.id}</span>
                      <span className="font-bold text-fg">{p.name}</span>
                      <StateBadge state={p.state} />
                      <span className="ml-auto text-[0.8rem] text-fg-muted">
                        {p.progress}%
                      </span>
                    </div>
                    <p className="mb-0 mt-2.5 text-[0.85rem] leading-relaxed text-fg-muted">
                      {p.blurb}
                    </p>
                    <ProgressBar value={p.progress} className="mt-4" />
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  )
}
