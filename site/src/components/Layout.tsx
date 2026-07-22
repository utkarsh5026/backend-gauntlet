import { NavLink, Outlet } from "react-router-dom";
import { cn } from "@/lib/utils";
import { REPO_URL } from "@/data/roadmap";

const nav = [
  { to: "/", label: "home", end: true },
  { to: "/method", label: "method" },
  { to: "/roadmap", label: "projects" },
];

export function Layout() {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-bg">
        <div className="mx-auto flex max-w-4xl items-center justify-between gap-4 px-5 py-3">
          <NavLink
            to="/"
            className="font-display text-[0.8rem] font-bold text-fg no-underline hover:text-accent"
          >
            <span className="text-fg-muted">~/</span>
            backend-gauntlet
          </NavLink>
          <nav className="flex items-center gap-1 text-[0.8rem]">
            {nav.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    "rounded-2xl px-2 py-1 no-underline",
                    isActive ? "bg-panel text-accent" : "text-fg-muted hover:text-fg",
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
              className="px-2 py-1 text-fg-muted no-underline hover:text-fg"
            >
              github ↗
            </a>
          </nav>
        </div>
      </header>

      <main className="mx-auto w-full max-w-4xl flex-1 px-5 py-10 sm:py-12">
        <Outlet />
      </main>

      <footer className="border-t border-line py-6">
        <div className="mx-auto flex max-w-4xl flex-col gap-1 px-5 text-[0.75rem] text-fg-muted sm:flex-row sm:items-center sm:justify-between">
          <span>
            <span className="text-accent-dim">[gauntlet]</span> built to learn — one primitive at a
            time
          </span>
          <span>rust · tokio · axum</span>
        </div>
      </footer>
    </div>
  );
}
