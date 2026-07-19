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
    <article className="mx-auto max-w-2xl space-y-12 animate-fade-up">
      <header className="space-y-4">
        <p className="font-mono text-[0.7rem] uppercase tracking-[0.2em] text-copper">
          Pedagogy
        </p>
        <h1 className="font-display text-4xl font-extrabold tracking-tight sm:text-5xl">
          How this lab works
        </h1>
        <p className="text-lg text-fg-muted">
          This is not a tutorial walkthrough and not a product demo. It is a
          progression of infrastructure primitives in Rust, designed so the owner
          writes the interesting code themselves.
        </p>
      </header>

      <ol className="space-y-8">
        {pillars.map((p, i) => (
          <li key={p.title} className="grid gap-2 sm:grid-cols-[3rem_1fr]">
            <span className="font-mono text-sm text-copper/80">
              {String(i + 1).padStart(2, '0')}
            </span>
            <div>
              <h2 className="font-display text-xl font-bold">{p.title}</h2>
              <p className="mt-1 text-fg-muted">{p.body}</p>
            </div>
          </li>
        ))}
      </ol>

      <section className="space-y-3 border-t border-line pt-10">
        <h2 className="font-display text-xl font-bold">What I refuse to do</h2>
        <ul className="list-disc space-y-2 pl-5 text-fg-muted">
          <li>Copy paste “full solutions” for vertical challenges</li>
          <li>Skip benches and call a feature done</li>
          <li>Log secrets or commit <code className="font-mono text-copper">.env</code></li>
          <li>Treat green <code className="font-mono text-copper">cargo check</code> as mastery</li>
        </ul>
      </section>

      <section className="space-y-3 border-t border-line pt-10">
        <h2 className="font-display text-xl font-bold">Why public</h2>
        <p className="text-fg-muted">
          Hiring and peer eyes should see how I think about scale — tradeoffs,
          failure modes, and proof — not a polished SaaS landing page for unfinished
          crates. The repo is the source of truth; this site is the reading room.
        </p>
      </section>
    </article>
  )
}
