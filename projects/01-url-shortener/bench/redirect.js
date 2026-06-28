// Redirect-hot-path load test for the URL shortener.
//
// Run via run.sh, or directly:
//   k6 run -e DIST=zipf -e N=10000 -e RATE=2000 bench/redirect.js
//
// ----------------------------------------------------------------------------
// The three things this script gets deliberately right (read these — they're
// the difference between a real benchmark and a misleading one):
//
// 1. NO REDIRECT FOLLOWING.  The endpoint answers 308 with a Location header.
//    k6 follows redirects by default — it would chase example.com over the
//    public internet and you'd be benchmarking the internet, not the service.
//    `{ redirects: 0 }` makes k6 record the 308 and stop.
//
// 2. OPEN MODEL (arrival rate), not closed (fixed VUs).  `constant-arrival-rate`
//    fires RATE requests/sec regardless of how fast the server answers. A
//    closed-loop test (N VUs each waiting for its reply) hides stalls —
//    "coordinated omission": when the server lags, the load generator politely
//    slows down, so the slow requests never make it into the latency tail.
//    Open model => a slow server visibly backs up and the tail tells the truth.
//
// 3. ZIPFIAN KEYS, not uniform.  Real shortener traffic is power-law: a few
//    viral links get most hits. Uniform-random keys would collapse the cache
//    hit ratio and make the cache look useless — a measurement artifact, not a
//    finding. DIST=zipf models that; the other DISTs are deliberate contrasts.
// ----------------------------------------------------------------------------

import http from "k6/http";
import { check } from "k6";
import { SharedArray } from "k6/data";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const N = parseInt(__ENV.N || "10000", 10); // seeded keyspace size
const PREFIX = __ENV.SLUG_PREFIX || "bench-";
const DIST = __ENV.DIST || "zipf"; // zipf | single | uniform | missing
const SKEW = parseFloat(__ENV.SKEW || "1.0"); // Zipf exponent s (higher = more skewed)
const RATE = parseInt(__ENV.RATE || "2000", 10); // target requests/sec
const DURATION = __ENV.DURATION || "30s";
const VUS = parseInt(__ENV.VUS || "200", 10); // pre-allocated workers

// Width must match seed.sh: zero-pad to the number of digits in N.
const WIDTH = String(N).length;
function slugForRank(rank) {
    return PREFIX + String(rank).padStart(WIDTH, "0");
}

// Precompute the Zipf cumulative distribution once, shared read-only across all
// VUs (SharedArray builds it a single time, not per-VU). cdf[k-1] = P(rank<=k).
const zipfCDF = new SharedArray("zipfCDF", function () {
    const cdf = new Array(N);
    let norm = 0;
    for (let k = 1; k <= N; k++) norm += 1 / Math.pow(k, SKEW);
    let acc = 0;
    for (let k = 1; k <= N; k++) {
        acc += 1 / Math.pow(k, SKEW) / norm;
        cdf[k - 1] = acc;
    }
    return cdf;
});

// Inverse-CDF sampling: draw u~U(0,1), binary-search the rank whose cumulative
// probability first exceeds u. O(log N) per request.
function sampleZipfRank() {
    const u = Math.random();
    let lo = 0,
        hi = N - 1;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (zipfCDF[mid] < u) lo = mid + 1;
        else hi = mid;
    }
    return lo + 1; // ranks are 1-indexed
}

function pickSlug() {
    switch (DIST) {
        case "single":
            return slugForRank(1); // A: one hot key -> pure cache
        case "uniform":
            return slugForRank(1 + Math.floor(Math.random() * N)); // worst case for the cache
        case "missing":
            return `${PREFIX}absent-${Math.floor(Math.random() * 1e9)}`; // D: never seeded -> 404
        case "zipf":
        default:
            return slugForRank(sampleZipfRank()); // B: realistic, skewed
    }
}

// Tell k6 which status is the success for this scenario so http_req_failed and
// the thresholds mean what we want: 308 for real redirects, 404 for the
// negative-cache (missing) scenario.
const EXPECTED = DIST === "missing" ? 404 : 308;
http.setResponseCallback(http.expectedStatuses(EXPECTED));

export const options = {
    discardResponseBodies: true,
    scenarios: {
        redirect: {
            executor: "constant-arrival-rate",
            rate: RATE,
            timeUnit: "1s",
            duration: DURATION,
            preAllocatedVUs: VUS,
            maxVUs: VUS * 5, // headroom: if the server slows, k6 spins up more workers to hold the rate
        },
    },
    thresholds: {
        http_req_failed: ["rate<0.01"], // <1% unexpected statuses
        http_req_duration: ["p(95)<50", "p(99)<200"], // tune to your machine after the first run
    },
};

export default function () {
    const slug = pickSlug();
    const res = http.get(`${BASE_URL}/${slug}`, { redirects: 0 });
    check(res, { "expected status": (r) => r.status === EXPECTED });
}
