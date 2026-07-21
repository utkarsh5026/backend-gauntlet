export type ProjectState = 'active' | 'paused' | 'blocked' | 'done' | 'not-started'

export type Project = {
  id: string
  slug: string
  name: string
  blurb: string
  state: ProjectState
  /** Rough overall progress 0–100 from `make status` at site scaffold time. */
  progress: number
  href?: string
}

export type Tier = {
  id: string
  label: string
  theme: string
  projects: Project[]
}

import { GENERATED_FOCUS } from './focus.generated'

export const REPO_URL = 'https://github.com/utkarsh5026/backend-gauntlet'

/**
 * Used only when generate-focus.ts couldn't determine a focus (e.g. a shallow
 * checkout with no merge history, or before the script has ever run).
 */
const FALLBACK_FOCUS = '01'

/**
 * The project the last `GENERATED_FOCUS.window` merged branches (that touched
 * a projects/ directory) were mostly about — see scripts/generate-focus.ts.
 * Recomputed at build/dev time, not hardcoded, so it can't go stale the way a
 * hand-edited "current focus" constant would.
 */
export const CURRENT_FOCUS = GENERATED_FOCUS?.id ?? FALLBACK_FOCUS

export const tiers: Tier[] = [
  {
    id: 't1',
    label: 'Tier 1 — Foundations',
    theme: 'async, I/O, protocols',
    projects: [
      {
        id: '01',
        slug: 'url-shortener',
        name: 'URL shortener + analytics',
        blurb: 'Snowflake IDs, cache-aside, async clicks, API keys',
        state: 'active',
        progress: 48,
        href: '/projects/01',
      },
      {
        id: '02',
        slug: 'rate-limiter',
        name: 'Distributed rate limiter',
        blurb: 'Token bucket + sliding window over gRPC',
        state: 'paused',
        progress: 0,
        href: '/projects/02',
      },
    ],
  },
  {
    id: 't2',
    label: 'Tier 2 — Concurrency & messaging',
    theme: 'fan-out, durability, backpressure',
    projects: [
      {
        id: '03',
        slug: 'realtime-pubsub',
        name: 'Real-time pub/sub + presence',
        blurb: 'WebSocket fan-out, backpressure',
        state: 'active',
        progress: 17,
        href: '/projects/03',
      },
      {
        id: '04',
        slug: 'job-queue',
        name: 'Distributed job queue',
        blurb: 'Durable jobs, retries, DLQ, SKIP LOCKED',
        state: 'active',
        progress: 75,
        href: '/projects/04',
      },
    ],
  },
  {
    id: 't3',
    label: 'Tier 3 — Storage & data',
    theme: 'ingest, blobs, caching',
    projects: [
      {
        id: '05',
        slug: 'metrics-pipeline',
        name: 'Time-series metrics pipeline',
        blurb: 'Ingest → ClickHouse → SSE dashboard',
        state: 'not-started',
        progress: 0,
        href: '/projects/05',
      },
      {
        id: '06',
        slug: 'object-store',
        name: 'S3-compatible object store',
        blurb: 'Multipart uploads, CAS blobs, streaming',
        state: 'active',
        progress: 94,
        href: '/projects/06',
      },
      {
        id: '07',
        slug: 'distributed-cache',
        name: 'Distributed cache',
        blurb: 'Consistent hashing, LRU/LFU, gossip',
        state: 'not-started',
        progress: 0,
        href: '/projects/07',
      },
    ],
  },
  {
    id: 't4',
    label: 'Tier 4 — The hard stuff',
    theme: 'logs, consensus, gateways',
    projects: [
      {
        id: '08',
        slug: 'message-broker',
        name: 'Mini message broker',
        blurb: 'Append-only log, partitions, consumer groups',
        state: 'not-started',
        progress: 0,
        href: '/projects/08',
      },
      {
        id: '09',
        slug: 'raft-kv',
        name: 'Distributed KV + Raft',
        blurb: 'Leader election, replication, snapshots',
        state: 'not-started',
        progress: 0,
        href: '/projects/09',
      },
      {
        id: '10',
        slug: 'api-gateway',
        name: 'API gateway',
        blurb: 'Routing, load balancing, circuit breaking, mTLS',
        state: 'not-started',
        progress: 0,
        href: '/projects/10',
      },
    ],
  },
  {
    id: 't5',
    label: 'Tier 5 — Multimedia & streaming',
    theme: 'VOD, live, realtime media',
    projects: [
      {
        id: '11',
        slug: 'vod-streaming',
        name: 'VOD streaming (HLS/DASH)',
        blurb: 'fMP4 segmenter, manifests, ABR',
        state: 'not-started',
        progress: 0,
        href: '/projects/11',
      },
      {
        id: '12',
        slug: 'transcode-pipeline',
        name: 'Transcoding pipeline',
        blurb: 'Chunked parallel transcode + job DAG',
        state: 'not-started',
        progress: 0,
        href: '/projects/12',
      },
      {
        id: '13',
        slug: 'live-ingest',
        name: 'Live ingest (RTMP → LL-HLS)',
        blurb: 'RTMP parse → low-latency HLS',
        state: 'not-started',
        progress: 0,
        href: '/projects/13',
      },
      {
        id: '14',
        slug: 'media-transport',
        name: 'Realtime media transport',
        blurb: 'RTP/RTCP, jitter buffer, NACK',
        state: 'not-started',
        progress: 0,
        href: '/projects/14',
      },
      {
        id: '15',
        slug: 'webrtc-sfu',
        name: 'WebRTC SFU',
        blurb: 'Selective forwarding, ICE, simulcast',
        state: 'not-started',
        progress: 0,
        href: '/projects/15',
      },
    ],
  },
  {
    id: 't6',
    label: 'Tier 6 — Capstones',
    theme: 'compose the stack',
    projects: [
      {
        id: '16',
        slug: 'live-platform',
        name: 'Live streaming platform',
        blurb: 'Ingest → ABR → LL-HLS → chat · k8s',
        state: 'not-started',
        progress: 0,
        href: '/projects/16',
      },
      {
        id: '17',
        slug: 'global-conferencing',
        name: 'Global WebRTC conferencing',
        blurb: 'Multi-region SFU federation',
        state: 'not-started',
        progress: 0,
        href: '/projects/17',
      },
    ],
  },
  {
    id: 't7',
    label: 'Tier 7 — Cross-cutting',
    theme: 'payments, P2P, search, engines',
    projects: [
      {
        id: '18',
        slug: 'ledger-payments-core',
        name: 'Ledger / payments core',
        blurb: 'Double-entry, idempotency, webhooks',
        state: 'not-started',
        progress: 0,
        href: '/projects/18',
      },
      {
        id: '19',
        slug: 'bittorrent',
        name: 'BitTorrent client',
        blurb: 'Peer wire, DHT, rarest-first',
        state: 'not-started',
        progress: 0,
        href: '/projects/19',
      },
      {
        id: '20',
        slug: 'full-text-search',
        name: 'Full-text search',
        blurb: 'Inverted index, BM25, shards',
        state: 'not-started',
        progress: 0,
        href: '/projects/20',
      },
      {
        id: '21',
        slug: 'workflow-engine',
        name: 'Workflow engine',
        blurb: 'Event-sourced replay, durable timers',
        state: 'not-started',
        progress: 0,
        href: '/projects/21',
      },
      {
        id: '22',
        slug: 'lsm-redis',
        name: 'LSM engine + Redis protocol',
        blurb: 'WAL, SSTables, compaction, RESP',
        state: 'not-started',
        progress: 0,
        href: '/projects/22',
      },
    ],
  },
]

export function allProjects(): Project[] {
  return tiers.flatMap((t) => t.projects)
}

export function findProject(id: string): Project | undefined {
  return allProjects().find((p) => p.id === id)
}

export function currentProject(): Project {
  return findProject(CURRENT_FOCUS) ?? allProjects()[0]!
}
