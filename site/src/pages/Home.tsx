import { Link } from "react-router-dom";
import { CURRENT_FOCUS, REPO_URL, allProjects, currentProject, tiers } from "@/data/roadmap";
import { ProgressBar } from "@/components/ProgressBar";
import { StateBadge } from "@/components/StateBadge";

/** A terminal-style readout of where the gauntlet stands, from real data. */
function computeStats() {
  const projects = allProjects();
  const furthest = projects.reduce((a, b) => (b.progress > a.progress ? b : a));
  return [
    { value: String(projects.length), label: "projects" },
    { value: String(tiers.length), label: "tiers" },
    {
      value: String(projects.filter((p) => p.state === "active").length),
      label: "in flight",
    },
    { value: `${furthest.progress}%`, label: `furthest · ${furthest.id}` },
  ];
}

export function Home() {
  const focus = currentProject();
  const stats = computeStats();

  return (
    <div className="space-y-16 sm:space-y-20">
      <section className="space-y-10 pt-4">
        <div className="space-y-6">
          <p className="cursor m-0 text-[0.85rem] text-fg-muted">
            <span className="text-accent">$</span> make status
          </p>

          <h1 className="font-display m-0 text-2xl font-bold leading-tight tracking-tight sm:text-3xl">
            backend-gauntlet
          </h1>

          <p className="m-0 max-w-[62ch] text-fg-muted">
            Twenty-two infrastructure primitives — queues, caches, brokers, consensus — built from
            scratch in Rust to learn how they really work. Scaffolded SPECs; the interesting logic
            left as <code className="text-warn">todo!()</code>.
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
        </div>

        <dl className="m-0 grid grid-cols-1 divide-y divide-line overflow-hidden rounded-md border border-line sm:grid-cols-4 sm:divide-x sm:divide-y-0">
          {stats.map((s) => (
            <div key={s.label} className="flex flex-col gap-1 px-5 py-4">
              <dd className="font-display m-0 text-[1.5rem] font-bold leading-none text-fg">
                {s.value}
              </dd>
              <dt className="text-[0.7rem] uppercase tracking-wide text-fg-muted">{s.label}</dt>
            </div>
          ))}
        </dl>
      </section>

      <section className="space-y-5">
        <h2 className="rule-title m-0 text-[0.95rem] font-bold">current focus</h2>
        <Link
          to={`/projects/${CURRENT_FOCUS}`}
          className="group block rounded-md border border-line bg-panel px-6 py-6 no-underline transition-colors hover:border-accent"
        >
          <div className="flex flex-wrap items-baseline justify-between gap-3">
            <span className="text-[1.05rem] font-bold text-fg transition-colors group-hover:text-accent">
              {focus.id} · {focus.name}
            </span>
            <StateBadge state={focus.state} />
          </div>
          <div className="mt-5 flex items-center gap-3">
            <ProgressBar value={focus.progress} className="flex-1" />
            <span className="text-[0.8rem] text-fg-muted">{focus.progress}%</span>
          </div>
          <p className="mb-0 mt-4 text-[0.85rem] leading-relaxed text-fg-muted">{focus.blurb}</p>
          <span className="mt-5 inline-block text-[0.8rem] text-fg-muted transition-colors group-hover:text-accent">
            open project →
          </span>
        </Link>
      </section>

      <section className="space-y-5">
        <h2 className="rule-title m-0 text-[0.95rem] font-bold">
          progress
          <span className="font-normal text-fg-muted">across all 22</span>
        </h2>
        <p className="m-0 max-w-2xl text-[0.85rem] text-fg-muted">
          The full board, rendered straight from <code className="text-fg">make status</code> —
          refreshed whenever a project moves.
        </p>
        <div className="panel p-3">
          <p className="panel-title">make status</p>
          <img
            src={`${import.meta.env.BASE_URL}status-dashboard.svg`}
            alt="backend-gauntlet progress dashboard across all projects"
            className="block w-full"
          />
        </div>
      </section>
    </div>
  );
}
