import { Link } from 'react-router-dom'
import { CURRENT_FOCUS, REPO_URL, currentProject } from '@/data/roadmap'
import { ProgressBar } from '@/components/ProgressBar'
import { StateBadge } from '@/components/StateBadge'

export function Home() {
  const focus = currentProject()

  return (
    <div className="space-y-14">
      <section className="space-y-6 pt-6">
        <p className="cursor m-0 text-[0.85rem] text-fg-muted">
          <span className="text-accent">$</span> make status
        </p>

        <h1 className="font-display m-0 text-2xl font-bold leading-tight tracking-tight sm:text-3xl">
          backend-gauntlet
        </h1>

        <p className="m-0 max-w-[62ch] text-fg-muted">
          Twenty-two infrastructure primitives — queues, caches, brokers,
          consensus — built from scratch in Rust to learn how they really work.
          Scaffolded SPECs; the interesting logic left as{' '}
          <code className="text-warn">todo!()</code>.
        </p>

        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-[0.85rem]">
          <Link to="/roadmap" className="text-accent">
            view the roadmap →
          </Link>
          <Link to="/method" className="text-fg-muted hover:text-fg">
            how I learn
          </Link>
          <a
            href={REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="text-fg-muted hover:text-fg"
          >
            source ↗
          </a>
        </div>
      </section>

      <section className="panel mt-4 px-6 py-6">
        <p className="panel-title">current focus</p>
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <Link
            to={`/projects/${CURRENT_FOCUS}`}
            className="text-[1.05rem] font-bold text-fg no-underline hover:text-accent"
          >
            {focus.id} · {focus.name}
          </Link>
          <StateBadge state={focus.state} />
        </div>
        <div className="mt-5 flex items-center gap-3">
          <ProgressBar value={focus.progress} className="flex-1" />
          <span className="text-[0.8rem] text-fg-muted">{focus.progress}%</span>
        </div>
        <p className="mb-0 mt-3 text-[0.85rem] leading-relaxed text-fg-muted">
          {focus.blurb}
        </p>
      </section>

      <section className="panel mt-4 p-3">
        <p className="panel-title">progress · from make status</p>
        <img
          src={`${import.meta.env.BASE_URL}status-dashboard.svg`}
          alt="backend-gauntlet progress dashboard across all projects"
          className="block w-full"
        />
      </section>
    </div>
  )
}
