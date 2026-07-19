# Request Routing — Longest-Prefix Match, Deterministically and Fast

> **What this teaches:** how a gateway decides *which* upstream gets a request —
> and why the obvious loop over routes is both ambiguous and too slow. No prior
> routing knowledge assumed. This prepares you for **V2** in [SPEC.md](../SPEC.md):
> the `match_request()` you'll build in [router.rs](../src/router.rs), over the
> route table that [config.rs](../src/config.rs) already loads and compiles.

---

## The one sentence to hold onto

**Routing must be a *rule*, not an accident: longest prefix wins, host and
method constrain, and the answer comes from a structure whose cost tracks the
path's length — not the table's size — because this runs on every single request.**

---

## 1. The problem: overlapping prefixes and who wins

A gateway config quickly grows routes that nest inside each other:

```json
{ "name": "api",     "path_prefix": "/api" }
{ "name": "api-v2",  "path_prefix": "/api/v2" }
{ "name": "static",  "path_prefix": "/" }
```

Now a request arrives for `GET /api/v2/users`. **All three prefixes match.**
Which route gets it?

| Candidate rule | Result for `/api/v2/users` | Verdict |
|---|---|---|
| First match in config order | Depends on file order — `api` if it's listed first | 💥 behavior changes when someone reorders a config file |
| Last match in config order | Also file-order-dependent | 💥 same bug, mirrored |
| **Longest prefix wins** | `api-v2` (`/api/v2` is longer than `/api` and `/`) | ✅ deterministic, order-independent, matches intuition ("most specific wins") |

The naive matcher —

```
for r in routes { if path.starts_with(r.path_prefix) { return r } }
```

— silently implements rule #1. It *works* in every demo, and then one day
someone alphabetizes the config file, `/api` moves above `/api/v2`, and every
v2 request quietly lands on the v1 pool. Nothing errors. That's the worst kind
of production incident: an *implicit* rule nobody knew they were depending on.

**Longest-prefix wins** is the explicit rule this vertical demands: with both
`/api` and `/api/v2` registered, `/api/v2/x` resolves to `/api/v2` — always,
regardless of insertion order. It's the same rule IP routers use for CIDR
routes, nginx uses for `location` blocks, and Kubernetes Ingress uses for
`Prefix` paths. "Most specific rule wins" is one of the great recurring ideas
in systems.

### A subtlety worth deciding on purpose: what is a "prefix"?

Is `/api` a prefix of `/apiary`? As a *string*, yes. As a *path*, most humans
say no — `/apiary` is a different resource, not something inside `/api`.

| Path | String-prefix match on `/api`? | Segment-aware match on `/api`? |
|---|---|---|
| `/api` | ✅ | ✅ |
| `/api/v2/users` | ✅ | ✅ |
| `/apiary` | ✅ 😬 | ❌ |

nginx's `location /api` does raw string-prefix matching; Kubernetes Ingress's
`Prefix` type matches *segment-wise*. Neither is "correct" — but you must pick
one, know you picked it, and test the `/apiary` case. This is exactly the kind
of decision `docs/10-design.md` wants recorded.

---

## 2. The second problem: the scan is O(routes), on the hot path of *everything*

Even with the precedence bug fixed (say, by scanning *all* routes and keeping
the longest match), the linear scan has a cost problem: it touches every route
for every request.

| Route count | Comparisons per request (scan) | At 50k req/s |
|---|---|---|
| 5 | 5 | trivial — the scan *wins* at this size |
| 1,000 | 1,000 | 50M prefix comparisons/sec |
| 10,000 | 10,000 | 500M prefix comparisons/sec — the matcher *is* your CPU profile |

Routing sits in front of literally every request the platform serves —
multiply any per-request cost by all of them. The trap named in
[CONCEPTS.md](../CONCEPTS.md): don't benchmark with 5 routes. The naive scan
wins at 5. The design question is the **curve**, and the SPEC's proof asks you
to show match latency staying roughly flat from 10 → 10,000 routes.

---

## 3. The design space: structures where cost tracks the *path*, not the table

The insight that unlocks sub-linear matching: the request path itself contains
everything needed to walk toward the answer. Two classic families:

### a) Sorted keys + binary search

Keep prefixes sorted. Binary search gets you to the *neighborhood* of the
request path in **O(log n)** string comparisons; candidates for
longest-prefix-match sit near that position (a prefix of your path sorts at or
before it). You then need a careful, small backward check to find the longest
registered prefix that actually prefixes the path. Compact, cache-friendly,
easy to rebuild — the subtle part is getting that neighborhood check *right*.

### b) A prefix tree (trie / radix tree)

Store routes as a tree keyed by path pieces (bytes, or path segments). Matching
*walks the request path* from the root, remembering the last node that carried
a route:

```
            (root)
              │ "/"
        ┌─────┴─────────┐
      "api"          "static"  ← route: static
        │ ● route: api
      "/v2"
        ● route: api-v2

  match "/api/v2/users":
    walk "/" → "api" (remember: api) → "/v2" (remember: api-v2)
    → "users" (no edge — stop)
    answer: last remembered = api-v2   ✅ longest prefix, no scan
```

The walk's cost is **O(path length)** — the same work whether the table holds
10 routes or 10,000, because you only ever follow the bytes the request gave
you. Longest-prefix-wins isn't an extra step; it *falls out* of remembering
the deepest route-carrying node you passed. This is the structure inside IP
routers (radix tries over address bits), axum's own router, and Envoy's
route-matching — and you built its cousin for project 06's key listings.

Either family satisfies the SPEC. The Done-when only demands the *property*
(sub-linear, deterministic longest-prefix); `docs/10-design.md` is where you
record which you chose and why. Choosing is the vertical — so this doc stops
here. `/hint` if you want nudges.

### Where host and method fit

Host and method are *constraints*, not part of the prefix walk:

| Request | Route `api-v2` (`host: api.example.com`, `methods: [POST]`) matches? |
|---|---|
| `POST api.example.com /api/v2/x` | ✅ |
| `GET api.example.com /api/v2/x` | ❌ method |
| `POST other.example.com /api/v2/x` | ❌ host |

The design question they add: when the longest prefix's constraints *fail*,
does a shorter prefix with passing constraints win, or is it a 404? (Real
gateways differ! nginx picks per-host `server` blocks *first*, then matches
paths within.) Decide, document, test. The scaffold's `Route` struct in
[router.rs](../src/router.rs) carries `host: Option<String>` and
`methods: Vec<Method>` (empty = any) — the semantics are yours to define
deterministically.

---

## 4. 404 vs 502/503: two different pages, two different teams

"No route matched" and "route matched but its backends are down" *feel*
similar — the client got an error — but they're operationally opposite:

| Signal | Meaning | Who investigates |
|---|---|---|
| **404 `NoRoute`** | The *client* asked for something the gateway doesn't front — a typo'd path, a stale SDK, a missing config entry | The caller / whoever owns the route table |
| **503 `NoHealthyBackend`** / 502 | The route is real; the *pool behind it* is broken | Whoever owns that backend |

[error.rs](../src/error.rs) already separates these variants. Collapsing them
into one generic error destroys the single most useful triage signal a gateway
produces. The Done-when explicitly requires the distinction.

---

## 5. Reloading the table without dropping requests

Configs change at runtime. The classic mistake is mutating the live table
under a lock — now every request contends on that lock (hot-path cost), and a
half-applied update can route inconsistently.

The pattern to internalize instead: **build the entire new table off to the
side, then swap the pointer atomically.**

```
              Arc<Router>  ──────▶  table v1
   in-flight requests hold ────────▲   (they finish on v1)

   reload: build table v2 fully, then swap the Arc

              Arc<Router>  ──────▶  table v2
   new requests load the Arc ──────▲   (they start on v2)
```

Requests already in flight keep their clone of the old `Arc` and finish
consistently on the old table; the old table is freed when the last of them
drops it. New requests see the new table. No lock on the hot path, no torn
state, no dropped requests — which is precisely the last Done-when box. Note
what makes this cheap: `Router::build` in [router.rs](../src/router.rs) is
already a pure *compile* step from config to table, so "build a whole new one"
costs nothing on the request path.

---

## Mental-model summary

| Concept | The model |
|---|---|
| Precedence | Longest prefix wins — a rule, never insertion order |
| "Prefix" | Decide string-wise vs segment-wise; test `/apiary` vs `/api` |
| Match cost | Must track path length, not route count — it runs on *every* request |
| Radix walk | Follow the path's own bytes; the deepest route node you passed *is* the answer |
| Host/method | Constraints layered on the prefix match — define what happens when they fail |
| 404 vs 502/503 | Misrouted client vs broken backend — different pages, keep them distinct |
| Reload | Build aside, swap an `Arc` atomically; in-flight finishes on the old table |

## Where you'll build this

**Module:** [src/router.rs](../src/router.rs) — the `todo!()` in
`match_request()`. The scaffold compiles config → `Vec<Route>` for you
(`Router::build`); the matching *structure* over those routes is the vertical.

**This doc unlocks V2's Done-when criteria** ([SPEC.md](../SPEC.md) §V2):
deterministic host+prefix+method resolution, longest-prefix precedence,
honoured constraints, a distinct 404, sub-linear match cost as the table grows
10 → 10k, and a reloadable table that doesn't drop in-flight requests.
