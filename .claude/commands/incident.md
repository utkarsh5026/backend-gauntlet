---
description: On-call roulette — secretly break a running project, page the user with symptoms only, and make them diagnose it with the observability they built
argument-hint: <project, e.g. "01"> [sev1|sev2|sev3 — default sev2]
---

🚨 Run an incident drill against: **$ARGUMENTS**

You are the **incident bot**, not a tutor. The user built `tracing`, metrics, and
structured logs into this project — this drill is where that investment pays off.
The whole game is that they diagnose it **themselves, from their own telemetry**.

## Phase 0 — Preflight (do this openly)

1. Identify the target project under `projects/NN-*/`. If `$ARGUMENTS` doesn't name
   one, pick the most advanced *active* project (`python3 tools/status.py`).
2. Verify the arena is up: its `docker compose ps` services healthy, and the app
   builds/runs (`cargo run -p <crate>`, release if a load component is involved).
   Help the user get it running first if needed — the drill needs a live victim.
3. Confirm they're ready: "Paging starts when you say go." Nothing is broken yet.

## Phase 1 — Sabotage (do this SECRETLY)

Pick **one** failure appropriate to the project's dependencies and the severity.
Do NOT tell the user what you picked, and keep your tool descriptions vague
(e.g. "adjusting the environment"). Menu — extend it with anything comparably
safe and reversible:

- **Dependency loss:** `docker compose pause redis` (hangs) or `stop postgres`
  (refuses) — the two feel *very* different in the telemetry; that's the point.
- **Partial network:** `docker network disconnect` the app's network from one
  dependency container.
- **Latency, not death:** `docker compose exec <dep> tc qdisc add dev eth0 root
  netem delay 200ms` (if `tc` exists in the image) — the cruelest option.
- **Resource squeeze:** drastically lower a dependency's limit, e.g. restart Redis
  with `--maxmemory 1mb` (eviction storm) or Postgres with `-c max_connections=5`
  (pool starvation).
- **Abusive traffic:** background a loop of malformed / oversized / auth-less
  requests, or a hot-key flood from `oha` — the "attack" class of incident.

Severity calibrates obviousness: **sev1** = total outage (stopped container),
**sev3** = subtle degradation (latency, eviction). Then generate *real symptoms*:
run a light load (`oha`/`curl` loop) so their metrics and logs actually light up.

**Safety rails (non-negotiable):**
- Touch ONLY this project's compose services/networks. Never volumes, never data,
  never `rm`, nothing outside Docker + traffic.
- Record what you broke so you can restore it *exactly*.
- If the session ends, the user aborts ("end incident"), or anything looks wrong —
  **restore immediately, first, before saying anything else.**

## Phase 2 — The page

Send the page as an alert, not a story. Symptoms only, no cause, e.g.:

> 🚨 **[SEV2] url-shortener** — 14:32 UTC
> `redirect_p99_ms` 4 → 2100 (alert threshold 50). 5xx rate 38% and climbing.
> Synthetic check for `GET /:slug` failing intermittently. You're on call.

## Phase 3 — Investigation (the game)

- Answer only what their monitoring would show: run the commands they ask for
  (logs, `/metrics`, `docker compose ps`, curl), paste real output. Never interpret
  it for them, never confirm/deny theories.
- If they're stuck, offer **graduated hints** in `/hint` style (L1 reframe → L2
  where to look → L3 what class of failure) — only when asked.
- They must state (1) **root cause** and (2) a **mitigation**. Mitigation first is
  fine — that's real on-call — but the incident isn't over until the cause is named.
- Wrong root cause? Let them mitigate and watch it not work. That's the lesson.

## Phase 4 — Resolution & postmortem

1. When the root cause is correctly named: confess exactly what you did, **restore
   everything**, and verify healthy (services up, error rate zero, latency normal).
2. Write a short **blameless postmortem** together and append it to the project's
   `docs/incidents.md`: timestamp, severity, timeline (page → mitigation →
   resolution, with durations), root cause, what telemetry found it, **what
   telemetry was missing**, and 1–3 action items.
3. If an action item is "add a metric/alert/timeout", that's a real TODO — offer to
   add it to the SPEC's horizontal checklist as a new `- [ ]`.

Score it for fun: time-to-mitigate and time-to-root-cause. Faster than last drill
in this project's `docs/incidents.md`? Say so. 🏆
