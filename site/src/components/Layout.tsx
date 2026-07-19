import { NavLink, Outlet } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { REPO_URL } from '@/data/roadmap'

const nav = [
  { to: '/', label: 'Home', end: true },
  { to: '/method', label: 'Method' },
  { to: '/roadmap', label: 'Roadmap' },
  { to: '/projects/01', label: 'Project 01' },
]

export function Layout() {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-20 border-b border-line/80 bg-bg/80 backdrop-blur-md">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-5 py-3.5">
          <NavLink
            to="/"
            className="font-display text-[1.05rem] font-bold tracking-tight text-fg no-underline hover:text-copper"
          >
            backend-gauntlet
          </NavLink>
          <nav className="flex flex-wrap items-center gap-1 sm:gap-2">
            {nav.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'rounded px-2.5 py-1.5 font-mono text-[0.7rem] uppercase tracking-[0.12em] no-underline transition-colors',
                    isActive
                      ? 'bg-copper/15 text-copper'
                      : 'text-fg-muted hover:text-fg',
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
            <a
              href={REPO_URL}
              target="_blank"
              rel="noreferrer"
              className="ml-1 rounded border border-line px-2.5 py-1.5 font-mono text-[0.7rem] uppercase tracking-[0.12em] text-fg-muted no-underline hover:border-copper/50 hover:text-copper"
            >
              GitHub
            </a>
          </nav>
        </div>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-5 py-10 sm:py-14">
        <Outlet />
      </main>

      <footer className="border-t border-line/80 py-8">
        <div className="mx-auto flex max-w-5xl flex-col gap-2 px-5 font-mono text-[0.7rem] text-fg-muted sm:flex-row sm:items-center sm:justify-between">
          <span>Built to learn. One primitive at a time.</span>
          <span className="text-copper/80">Rust · Tokio · Axum</span>
        </div>
      </footer>
    </div>
  )
}
