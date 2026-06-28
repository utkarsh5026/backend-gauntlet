#!/usr/bin/env node
//
// Zero-install sanity check (just Node).   ->  node bench/smoke.js
//
// Proves the redirect contract the load test relies on, before you bother
// installing k6: it creates one link through the real API, then fetches the
// redirect WITHOUT following it — you should see a bare 308 and a location
// header. fetch's default is to FOLLOW redirects (chasing example.com off-site);
// `redirect: "manual"` is the Node equivalent of k6's `{ redirects: 0 }`.

const { loadEnv } = require("./_lib");

loadEnv();

const BASE_URL = process.env.BASE_URL || "http://localhost:8080";
// First key from API_KEYS (the .env stand-in), or override with API_KEY=...
const API_KEY = process.env.API_KEY || (process.env.API_KEYS || "dev-secret-key").split(",")[0];

(async () => {
  console.log("1) health check");
  const h = await fetch(`${BASE_URL}/healthz`);
  console.log(`   -> ${h.status} ${await h.text()}`);

  console.log("\n2) create a link");
  const created = await fetch(`${BASE_URL}/api/links`, {
    method: "POST",
    headers: { authorization: `Bearer ${API_KEY}`, "content-type": "application/json" },
    body: JSON.stringify({ url: "https://example.com/smoke-test" }),
  });
  const body = await created.json();
  console.log(`   ${JSON.stringify(body)}`);
  const slug = body.slug;
  if (!slug) {
    console.error("   could not get a slug from the response — is the API key right?");
    process.exit(1);
  }

  console.log("\n3) redirect WITHOUT following (this is what k6 does):");
  const res = await fetch(`${BASE_URL}/${slug}`, { redirect: "manual" });
  console.log(`   status:   ${res.status}`);
  console.log(`   location: ${res.headers.get("location")}`);
  console.log("\n   (the default `fetch` would follow it off-site — the trap the load test avoids.)");
  console.log("\nsmoke ok. install k6 and run `node bench/run.js` for the real thing.");
})().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
