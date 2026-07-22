import { Link, Navigate, useParams } from "react-router-dom";
import { getProject, projectLinks } from "@/data/projects";
import { findProject } from "@/data/roadmap";
import { StateBadge } from "@/components/StateBadge";
import { AssetGallery } from "@/components/AssetGallery";
import { Reveal } from "@/components/Reveal";

/** First sentence as the hook; the rest goes behind a disclosure. */
function splitLead(text: string): [string, string] {
  const match = text.match(/^.*?[.!?]["”')\]]*(?=\s|$)/);
  if (!match) return [text, ""];
  return [match[0], text.slice(match[0].length).trim()];
}

export function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const detail = id ? getProject(id) : undefined;
  const meta = id ? findProject(id) : undefined;

  if (!detail) {
    return <Navigate to="/" replace />;
  }

  const links = projectLinks(detail);
  const [problemLead, problemRest] = splitLead(detail.problem);

  return (
    <article className="space-y-14">
      <header className="max-w-2xl space-y-4">
        <p className="m-0 flex flex-wrap items-baseline gap-3 text-[0.85rem] text-fg-muted">
          <span>
            <span className="text-accent">$</span> cat projects/{detail.id}-{detail.slug}/SPEC.md
          </span>
          {meta && <StateBadge state={meta.state} />}
        </p>
        <h1 className="font-display m-0 text-xl font-bold tracking-tight sm:text-2xl">
          {detail.title}
        </h1>
        <p className="m-0 text-fg-muted">{detail.tagline}</p>
        <p className="m-0 flex flex-wrap gap-x-5 gap-y-1 pt-1 text-[0.85rem]">
          <a href={links.spec} target="_blank" rel="noreferrer">
            SPEC.md ↗
          </a>
          <a
            href={links.code}
            target="_blank"
            rel="noreferrer"
            className="text-fg-muted hover:text-fg"
          >
            source ↗
          </a>
          <Link to="/roadmap" className="text-fg-muted no-underline hover:text-fg">
            ← all projects
          </Link>
        </p>
      </header>

      <section className="max-w-2xl space-y-4">
        <h2 className="rule-title m-0 text-[1rem] font-bold">problem</h2>
        <p className="m-0 leading-relaxed text-fg">{problemLead}</p>
        {problemRest && (
          <Reveal label="the full problem">
            <p className="m-0 leading-relaxed text-fg-muted">{problemRest}</p>
          </Reveal>
        )}
        {detail.whatItDoes.length > 0 && (
          <ul className="m-0 list-none space-y-2 p-0 pt-1 text-[0.85rem] text-fg-muted">
            {detail.whatItDoes.map((line) => (
              <li key={line}>
                <span className="mr-2 text-accent-dim" aria-hidden>
                  -
                </span>
                <code className="text-fg">{line}</code>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-4">
        <h2 className="rule-title m-0 text-[1rem] font-bold">mental model</h2>
        <div className="flex flex-wrap items-center gap-y-2">
          {detail.verticals.map((step, i) => (
            <div key={step.id} className="flex items-center">
              <div className="rounded-md border border-line bg-panel px-4 py-2.5">
                <p className="m-0 text-[0.7rem] text-accent">{step.id}</p>
                <p className="m-0 mt-0.5 text-[0.85rem] font-medium text-fg">{step.title}</p>
              </div>
              {i < detail.verticals.length - 1 && (
                <span className="px-2 text-fg-muted" aria-hidden>
                  →
                </span>
              )}
            </div>
          ))}
        </div>
      </section>

      <AssetGallery projectDir={`${detail.id}-${detail.slug}`} />

      <section className="space-y-5">
        <h2 className="rule-title m-0 text-[1rem] font-bold">
          verticals
          <span className="text-[0.75rem] font-normal text-fg-muted">concepts to internalize</span>
        </h2>
        <ul className="m-0 list-none space-y-7 p-0">
          {detail.verticals.map((v) => (
            <li
              key={v.id}
              className="grid gap-1 border-l border-accent-dim py-1 pl-5 sm:grid-cols-[3.5rem_1fr]"
            >
              <span className="text-[0.85rem] text-accent">{v.id}</span>
              <div>
                <h3 className="m-0 text-[0.95rem] font-bold">{v.title}</h3>
                <p className="mb-0 mt-2 text-[0.9rem] leading-relaxed text-fg-muted">{v.concept}</p>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="max-w-2xl space-y-3">
        <h2 className="rule-title m-0 text-[1rem] font-bold">horizontals</h2>
        <ul className="m-0 list-none space-y-3 p-0 text-[0.9rem] leading-relaxed text-fg-muted">
          {detail.horizontals.map((h) => (
            <li key={h}>
              <span className="mr-2 text-accent-dim" aria-hidden>
                -
              </span>
              {h}
            </li>
          ))}
        </ul>
      </section>

      {detail.boss && (
        <section className="panel mt-2 max-w-2xl px-6 py-6">
          <p className="panel-title text-warn">🐉 boss fight</p>
          <h2 className="m-0 text-[1.05rem] font-bold">{detail.boss.name}</h2>
          <p className="mb-0 mt-3 text-[0.9rem] leading-relaxed text-fg-muted">
            {detail.boss.idea}
          </p>
        </section>
      )}
    </article>
  );
}
