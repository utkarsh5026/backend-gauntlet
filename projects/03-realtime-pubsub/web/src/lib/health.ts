// Devtools dependency-health client. Mirrors the Rust `DepsHealth` from
// routes.rs (`GET /debug/health`): a live up/down probe of the OPTIONAL backing
// stores — the Postgres admin roster and the Redis cross-node bus. This is the
// readiness view, distinct from the bare `/healthz` liveness probe.
//
// Keep these types in lockstep with the `DepStatus` / `DepsHealth` structs in
// projects/03-realtime-pubsub/src/routes.rs.

export type DepState = "up" | "down" | "disabled";

export interface DepStatus {
    state: DepState;
    /** Error text on `down`, reason on `disabled`; absent when `up`. */
    detail?: string;
    /** Probe round-trip in ms; present only when `up`. */
    latency_ms?: number;
}

export interface DepsHealth {
    /** Postgres (admin roster). `disabled` when DATABASE_URL is unset. */
    db: DepStatus;
    /** Redis (cross-node bus). Probed even in single-node mode. */
    redis: DepStatus;
    /** Whether the server is actually bridging through Redis (CLUSTER=true). */
    cluster_mode: boolean;
    /** Whether WS_AUTH_TOKEN is set. When false, every /ws upgrade is rejected
     *  with 401 (fail closed) and no one can come online. The server reports only
     *  this boolean, never the secret. */
    ws_auth_configured: boolean;
}

/** Probe the backend. Throws on network error or non-2xx (server unreachable). */
export async function fetchDepsHealth(
    signal?: AbortSignal,
): Promise<DepsHealth> {
    const res = await fetch("/debug/health", { signal });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return (await res.json()) as DepsHealth;
}
