import type { ReactNode } from 'react'
import { useEffect, useState } from 'react'
import { Database, HeartPulse, KeyRound, Server } from 'lucide-react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { fetchDepsHealth, type DepState, type DepsHealth } from '@/lib/health'

// How often to re-probe the backend for dependency health. Slow on purpose:
// each poll opens a fresh Redis connection server-side, so this is a status
// light, not a hot path.
const POLL_MS = 4000

// `unknown` is a frontend-only state: the server itself is unreachable, so we
// can't say anything about its dependencies.
type Wire = DepState | 'unknown'

const DOT: Record<Wire, string> = {
  up: 'bg-success',
  down: 'bg-destructive',
  disabled: 'bg-muted-foreground/40',
  unknown: 'bg-muted-foreground/40',
}

function label(state: Wire, latencyMs?: number): string {
  switch (state) {
    case 'up':
      return latencyMs != null ? `${latencyMs.toFixed(1)} ms` : 'up'
    case 'down':
      return 'down'
    case 'disabled':
      return 'disabled'
    case 'unknown':
      return '—'
  }
}

function Row({
  icon,
  name,
  state,
  detail,
  latencyMs,
  value,
}: {
  icon: ReactNode
  name: string
  state: Wire
  detail?: string
  latencyMs?: number
  /** Overrides the state-derived text (e.g. "on"/"off" for a config flag). */
  value?: string
}) {
  return (
    <div className="flex items-center gap-2">
      <span className={cn('size-2 shrink-0 rounded-full', DOT[state], state === 'up' && 'animate-pulse')} />
      <span className="text-muted-foreground flex items-center gap-1.5 text-xs">
        {icon}
        {name}
      </span>
      <span
        className={cn(
          'ml-auto font-mono text-[11px] tabular-nums',
          state === 'down' ? 'text-destructive' : 'text-foreground',
        )}
        title={detail}
      >
        {value ?? label(state, latencyMs)}
      </span>
    </div>
  )
}

/**
 * Live up/down light for the server's optional backing stores. Self-contained:
 * polls `GET /debug/health` on its own timer, holds its own state — nothing to
 * thread through the chat store.
 */
export function DepsHealth() {
  const [health, setHealth] = useState<DepsHealth | null>(null)
  const [reachable, setReachable] = useState(true)

  useEffect(() => {
    let alive = true
    const ctrl = new AbortController()

    const tick = async () => {
      try {
        const h = await fetchDepsHealth(ctrl.signal)
        if (!alive) return
        setHealth(h)
        setReachable(true)
      } catch {
        if (!alive || ctrl.signal.aborted) return
        setReachable(false)
      }
    }

    void tick()
    const id = setInterval(() => void tick(), POLL_MS)
    return () => {
      alive = false
      ctrl.abort()
      clearInterval(id)
    }
  }, [])

  const db = reachable && health ? health.db : { state: 'unknown' as Wire }
  const redis = reachable && health ? health.redis : { state: 'unknown' as Wire }
  const idleRedis = reachable && health?.redis.state === 'up' && !health.cluster_mode

  // WS auth is a config gate, not a probed store: "on" (green) when the secret is
  // set, "off" (red) when unset — because unset means every /ws upgrade is 401'd.
  const wsState: Wire = reachable && health ? (health.ws_auth_configured ? 'up' : 'down') : 'unknown'
  const wsValue = wsState === 'unknown' ? undefined : health!.ws_auth_configured ? 'on' : 'off'
  const wsAuthMissing = reachable && health && !health.ws_auth_configured

  return (
    <Card className="gap-3 py-4">
      <CardHeader className="gap-0 px-4 [.border-b]:pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <HeartPulse className="size-4" /> Dependencies
          {!reachable && (
            <span className="text-destructive ml-auto text-[10px] font-medium tracking-wide uppercase">
              server down
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5 px-4">
        <Row
          icon={<Database className="size-3" />}
          name="Postgres"
          state={db.state}
          detail={'detail' in db ? db.detail : undefined}
          latencyMs={'latency_ms' in db ? db.latency_ms : undefined}
        />
        <Row
          icon={<Server className="size-3" />}
          name="Redis"
          state={redis.state}
          detail={'detail' in redis ? redis.detail : undefined}
          latencyMs={'latency_ms' in redis ? redis.latency_ms : undefined}
        />
        <Row
          icon={<KeyRound className="size-3" />}
          name="WS auth"
          state={wsState}
          value={wsValue}
          detail={wsAuthMissing ? 'WS_AUTH_TOKEN unset — all /ws upgrades 401' : undefined}
        />

        {wsAuthMissing && (
          <p className="text-destructive text-[10px] leading-relaxed">
            <span className="font-mono">WS_AUTH_TOKEN</span> unset — every <span className="font-mono">/ws</span>{' '}
            upgrade is rejected (401), so no one can come online. Set it in{' '}
            <span className="font-mono">.env</span> and type the same value into the ws token field.
          </p>
        )}
        {idleRedis && (
          <p className="text-muted-foreground text-[10px] leading-relaxed">
            Redis reachable but idle — the app only bridges through it in cluster mode (V4 /{' '}
            <span className="font-mono">CLUSTER=true</span>).
          </p>
        )}
        {reachable && health?.db.state === 'disabled' && (
          <p className="text-muted-foreground text-[10px] leading-relaxed">
            Roster off — set <span className="font-mono">DATABASE_URL</span> to enable the{' '}
            <span className="font-mono">/admin</span> panel.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
