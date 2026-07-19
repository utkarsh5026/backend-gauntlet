export const project01 = {
  id: '01',
  title: 'URL Shortener + Analytics',
  tagline:
    'A URL shortener is the hello world of backend — but the scalable version is anything but.',
  problem:
    'Read-heavy redirects, unique IDs without coordination, bursty click traffic, and a link that goes viral must not take the service down. Project 01 is the first rung: ship a shortener that behaves like a real system under load.',
  whatItDoes: [
    'POST /api/links with a long URL → short slug',
    'GET /{slug} → 301/302 redirect',
    'GET /api/links/{slug}/stats → click count + recent analytics',
    'API-key auth on write/stats; redirects stay public',
  ],
  flow: [
    { id: 'create', label: 'Create', detail: 'Mint ID → slug → persist' },
    { id: 'redirect', label: 'Redirect', detail: 'Hot path: lookup → 302' },
    { id: 'cache', label: 'Cache', detail: 'Cache-aside + stampede guard' },
    { id: 'ingest', label: 'Ingest', detail: 'Async click batching' },
  ],
  verticals: [
    {
      id: 'V1',
      title: 'Distributed ID generation',
      concept:
        'Why coordination-free IDs matter — Snowflake-style 64-bit IDs vs UUIDv4 (random, not sortable) vs DB sequences (coordinated bottlenecks).',
      module: 'src/id_gen.rs',
    },
    {
      id: 'V2',
      title: 'Cache-aside with stampede protection',
      concept:
        'Redirects must not hit Postgres every time. Internalize cache-aside vs write-through vs write-behind, and why stampedes are a real outage cause.',
      module: 'src/cache.rs',
    },
    {
      id: 'V3',
      title: 'Async click ingestion',
      concept:
        'Analytics must never slow the redirect. Bounded channels, explicit overflow policy, batching, and graceful flush on shutdown.',
      module: 'src/ingest.rs',
    },
  ],
  horizontals: [
    'Protocols: deliberate 301/302, Cache-Control / ETag, graceful shutdown',
    'Caching: jittered TTLs, negative cache, documented stampede strategy',
    'Security: API keys (src/auth.rs), URL/SSRF validation, sqlx-checked queries',
    'Observability: request spans, structured redirect logs, /metrics counters',
  ],
  boss: {
    name: 'The Thundering Herd',
    idea: 'A cold hot-key stampede after cache expiry — numeric targets for RPS, p99, hit ratio, and ≤1 Postgres rebuild under concurrent race. Proof lives in bench/ and docs, not vibes.',
  },
  links: {
    spec: 'https://github.com/utkarsh5026/backend-gauntlet/blob/master/projects/01-url-shortener/SPEC.md',
    code: 'https://github.com/utkarsh5026/backend-gauntlet/tree/master/projects/01-url-shortener',
  },
} as const
