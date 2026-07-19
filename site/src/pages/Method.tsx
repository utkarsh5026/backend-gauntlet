const pillars = [
  {
    title: 'Two-axis SPECs',
    body: "Every project grades on verticals (build the hard core from scratch — the parts you'd normally cargo add) and horizontals (protocols, caching, security, observability woven into the same ticket).",
  },
  {
    title: 'todo!() is the worklist',
    body: 'Scaffolds compile; interesting logic panics at runtime on purpose. The gap between a clean cargo check and a defeated boss fight is where the learning lives.',
  },
  {
    title: 'Observable "done"',
    body: 'Criteria are outcomes you can prove — tests, benches, design docs — never solution steps. A checkbox flips only when its Proof exists.',
  },
  {
    title: 'Boss fights',
    body: "Each project ends with a named load/failure scenario and numeric targets (RPS, p99, hit ratio). Vibes don't count; reproducible numbers do.",
  },
]

export function Method() {
  return (
    <article className="mx-auto max-w-2xl space-y-12">
      <header className="space-y-3">
        <p className="m-0 text-[0.85rem] text-fg-muted">
          <span className="text-accent">$</span> cat METHOD.md
        </p>
        <h1 className="font-display m-0 text-xl font-bold tracking-tight sm:text-2xl">
          how this lab works
        </h1>
        <p className="m-0 text-fg-muted">
          Not a tutorial walkthrough, not a product demo. A progression of
          infrastructure primitives in Rust, designed so the owner writes the
          interesting code themselves.
        </p>
      </header>

      <ul className="m-0 list-none space-y-9 p-0">
        {pillars.map((p) => (
          <li key={p.title}>
            <h2 className="m-0 text-[1rem] font-bold">
              <span className="mr-2 text-accent" aria-hidden>
                ▸
              </span>
              {p.title}
            </h2>
            <p className="mb-0 mt-2.5 pl-5 leading-relaxed text-fg-muted">
              {p.body}
            </p>
          </li>
        ))}
      </ul>

      <section className="panel mt-2 px-6 py-6">
        <p className="panel-title">what I refuse to do</p>
        <ul className="m-0 list-none space-y-2.5 p-0 text-fg-muted">
          <li>
            <span className="mr-2 text-err">✕</span>copy-paste “full solutions”
            for vertical challenges
          </li>
          <li>
            <span className="mr-2 text-err">✕</span>skip benches and call a
            feature done
          </li>
          <li>
            <span className="mr-2 text-err">✕</span>log secrets or commit{' '}
            <code className="text-fg">.env</code>
          </li>
          <li>
            <span className="mr-2 text-err">✕</span>treat a green{' '}
            <code className="text-fg">cargo check</code> as mastery
          </li>
        </ul>
      </section>

      <section className="space-y-2">
        <h2 className="rule-title m-0 text-[1rem] font-bold">why public</h2>
        <p className="m-0 text-fg-muted">
          Hiring and peer eyes should see how I think about scale — tradeoffs,
          failure modes, and proof — not a polished SaaS landing page for
          unfinished crates. The repo is the source of truth; this site is the
          reading room.
        </p>
      </section>
    </article>
  )
}
