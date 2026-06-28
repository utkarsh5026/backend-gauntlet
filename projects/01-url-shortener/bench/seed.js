#!/usr/bin/env node
//
// Bulk-seed N benchmark links straight into Postgres.   ->  node bench/seed.js
//
// Why not POST them through /api/links? That path is rate-limited (burst 10,
// ~5/s) on purpose — seeding 10k links that way would take half an hour. The
// keyspace is just fixtures, so we INSERT directly and skip the HTTP layer.
//
// Slugs are deterministic and zero-padded:  bench-00001 .. bench-N
// The pad WIDTH = number of digits in N, and redirect.js computes the SAME
// width, so the two sides always agree on the keyspace. Keep N consistent
// between seed and run (run.js passes one N to both).
//
// IDs are NEGATIVE (-1 .. -N): real Snowflake ids are large positives, so
// negatives can never collide with a link created through the real API.

const { loadEnv, psqlExec, psqlQuery } = require("./_lib");

loadEnv();

const N = parseInt(process.env.N || "10000", 10);
const PREFIX = process.env.SLUG_PREFIX || "bench-";
const WIDTH = String(N).length; // MUST match redirect.js

const pad = (i) => String(i).padStart(WIDTH, "0");
console.log(`seeding ${N} links (${PREFIX}${pad(1)} .. ${PREFIX}${pad(N)}) ...`);

psqlExec(`
INSERT INTO links (id, slug, long_url)
SELECT -i,                                              -- negative: never collides with a real Snowflake id
       '${PREFIX}' || lpad(i::text, ${WIDTH}, '0'),
       'https://example.com/dest/' || i                -- never actually fetched (k6 disables redirect-following)
FROM   generate_series(1, ${N}) AS g(i)
ON CONFLICT (slug) DO NOTHING;
`);

const total = psqlQuery(`SELECT count(*) FROM links WHERE slug LIKE '${PREFIX}%';`);
console.log(`done. bench links in db: ${total}`);
