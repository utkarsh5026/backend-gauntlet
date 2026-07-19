export type ProjectVertical = {
  id: string
  title: string
  concept: string
}

export type ProjectDetail = {
  id: string
  slug: string
  title: string
  tagline: string
  problem: string
  whatItDoes: string[]
  verticals: ProjectVertical[]
  horizontals: string[]
  boss?: { name: string; idea: string }
}

const REPO = 'https://github.com/utkarsh5026/backend-gauntlet'

export const projectDetails: ProjectDetail[] = [
  {
    id: '01',
    slug: 'url-shortener',
    title: 'URL Shortener + Analytics',
    tagline: 'A URL shortener is the "hello world" of backend — but the scalable version is anything but. It\'s read-heavy (every redirect is a lookup), needs unique IDs without coordination, has to absorb…',
    problem: 'A URL shortener is the "hello world" of backend — but the scalable version is anything but. It\'s read-heavy (every redirect is a lookup), needs unique IDs without coordination, has to absorb bursty click traffic, and must not fall over when a link goes viral. That makes it the perfect first rung.',
    whatItDoes: [
      'POST /api/links with a long URL → returns a short slug (e.g. aZ3kQ).',
      'GET /{slug} → 301/302 redirect to the original URL.',
      'GET /api/links/{slug}/stats → click count + recent analytics.',
      'API-key auth on the write/stats endpoints; redirects are public.',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Distributed ID generation',
        concept: 'Coordination-free Snowflake-style IDs vs UUIDv4 vs DB sequences.',
      },
      {
        id: 'V2',
        title: 'Cache-aside with stampede protection',
        concept: 'Cache-aside vs write-through; stampedes are real outage causes.',
      },
      {
        id: 'V3',
        title: 'Async click ingestion',
        concept: 'Backpressure, batching, and trading exactness for redirect latency.',
      },
    ],
    horizontals: [
      'Protocols: deliberate 301/302, Cache-Control / ETag, graceful shutdown',
      'Caching: jittered TTLs, negative cache, documented stampede strategy',
      'Security: API keys, URL/SSRF validation, sqlx-checked queries',
      'Observability: request spans, structured redirect logs, /metrics',
    ],
    boss: {
      name: 'The Thundering Herd',
      idea: 'Cold hot-key stampede after cache expiry — RPS, p99, hit ratio, and ≤1 Postgres rebuild under concurrent race.',
    },
  },
  {
    id: '02',
    slug: 'rate-limiter',
    title: 'Distributed Rate Limiter',
    tagline: 'A rate limiter looks trivial: "allow N requests per second". The trap is that the interesting version runs on many gateway instances at once, all sharing one source of truth, and has to make a…',
    problem: 'A rate limiter looks trivial: "allow N requests per second". The trap is that the interesting version runs on many gateway instances at once, all sharing one source of truth, and has to make a correct allow/deny decision in well under a millisecond on the hot path — without letting two concurrent requests both "see" the last remaining token. It\'s a small algorithm wrapped in a hard distributed-systems and concurrency problem. That\'s the rung.',
    whatItDoes: [
      'A gRPC service with a single hot method: Check(key, cost) → {allowed, remaining, retry_after}.',
      'key is whatever you limit on: an API key, user id, or client IP.',
      'A Peek(key) method that reports state without consuming budget.',
      'Limits are configured per key (a global default to start; per-tier later).',
      'It is the thing that other services (e.g. project 01\'s POST /api/links) call',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Token bucket',
        concept: 'Token bucket without a background refill timer — math on the hot path.',
      },
      {
        id: 'V2',
        title: 'Sliding window',
        concept: 'Sliding window fixes the fixed-window boundary burst.',
      },
      {
        id: 'V3',
        title: 'Distributed atomicity',
        concept: 'Correct allow/deny across many instances needs atomic shared state.',
      },
    ],
    horizontals: [
      'Protocols: gRPC Check/Peek with clear allow/deny semantics',
      'Caching: shared limiter state as the source of truth',
      'Security: key identity, no secret leakage in errors',
      'Observability: allow/deny counters, remaining budget, latency',
    ],
  },
  {
    id: '03',
    slug: 'realtime-pubsub',
    title: 'Real-time Pub/Sub + Presence',
    tagline: 'A broadcast server looks trivial: "a client subscribes to a topic, and every message published to that topic is sent to every subscriber." The trap is what real-time and at scale do to that…',
    problem: 'A broadcast server looks trivial: "a client subscribes to a topic, and every message published to that topic is sent to every subscriber." The trap is what real-time and at scale do to that sentence. WebSockets are long-lived and stateful, so the server now holds thousands of open sockets at once. Some of those clients read slowly — and a single slow reader must not be allowed to stall the fast ones or balloon the server\'s memory (backpressure). And the moment you run more than one server instance, a message…',
    whatItDoes: [
      'JSON subscribe/unsubscribe/publish over the socket',
      'Presence join/leave visible to topic peers',
      'Multi-instance fan-out across nodes',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'The fan-out hub',
        concept: 'In-process pub/sub hub: topics, subscribers, fan-out.',
      },
      {
        id: 'V2',
        title: 'Backpressure',
        concept: 'A slow reader must not stall fast peers or balloon memory.',
      },
      {
        id: 'V3',
        title: 'Presence',
        concept: 'Presence is soft state with join/leave/timeout lifecycle.',
      },
      {
        id: 'V4',
        title: 'Multi-node fan-out',
        concept: 'Publish on node A must reach subscribers on node B.',
      },
    ],
    horizontals: [
      'Protocols: WebSocket JSON subscribe/publish',
      'Caching: soft presence state with TTLs',
      'Security: auth on connect / topic ACL basics',
      'Observability: fan-out lag, dropped messages, connection counts',
    ],
  },
  {
    id: '04',
    slug: 'job-queue',
    title: 'Distributed Job Queue',
    tagline: '"Put a job on a queue, have a worker run it later." It sounds like a wrapper around a database table — INSERT a row, SELECT it back, run it. The trap is everything that the words distributed,…',
    problem: '"Put a job on a queue, have a worker run it later." It sounds like a wrapper around a database table — INSERT a row, SELECT it back, run it. The trap is everything that the words distributed, durable, and at-least-once smuggle in. The moment you run more than one worker, two of them will SELECT the same row and run the job twice — unless the dequeue is a single atomic step that hands each job to exactly one worker (SKIP LOCKED). The moment a worker crashes mid-job, that job is neither done nor available to…',
    whatItDoes: [
      'Enqueue a job with payload and optional delay',
      'Workers claim jobs atomically (SKIP LOCKED)',
      'Retries with backoff; exhausted jobs land in a DLQ',
      'Complete/fail APIs + queue depth metrics',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'The claim engine',
        concept: 'Atomic claim with SKIP LOCKED — one job, one worker.',
      },
      {
        id: 'V2',
        title: 'Visibility timeout',
        concept: 'Leases/visibility timeouts make at-least-once real under crashes.',
      },
      {
        id: 'V3',
        title: 'Retries with backoff + the dead-letter queue',
        concept: 'Backoff + dead-letter so poison jobs don\'t hot-loop forever.',
      },
      {
        id: 'V4',
        title: 'Scheduling + LISTEN/NOTIFY',
        concept: 'Wake workers without busy-polling an empty table.',
      },
    ],
    horizontals: [
      'Protocols: enqueue/claim/complete HTTP (or similar) API',
      'Caching: optional result memoization — not the durability layer',
      'Security: enqueue tokens, no secret logging',
      'Observability: queue depth, running, DLQ, lease lag',
    ],
  },
  {
    id: '05',
    slug: 'metrics-pipeline',
    title: 'Time-series Metrics Pipeline',
    tagline: '"Take in a firehose of numbers, store them, and draw a graph." It sounds like an INSERT and a SELECT ... GROUP BY. The trap is the shape of the load. A metrics pipeline ingests millions of points…',
    problem: '"Take in a firehose of numbers, store them, and draw a graph." It sounds like an INSERT and a SELECT ... GROUP BY. The trap is the shape of the load. A metrics pipeline ingests millions of points per second — tiny, append- only, never updated — and is then asked to answer "p99 latency for service=api, region=us, last 6 hours" in under a second. That is two systems fighting each other: a write path that must never block on the read path, and a read path that can\'t possibly scan a trillion raw points per query.…',
    whatItDoes: [
      'Ingest metrics via line protocol (HTTP and/or UDP)',
      'Roll up into time buckets with mergeable sketches',
      'Query recent windows; stream live updates over SSE',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'The ingest parser + the time-series data model',
        concept: 'What a metric is: name, tags, timestamp, value — and cardinality.',
      },
      {
        id: 'V2',
        title: 'The rollup engine',
        concept: 'You cannot average percentiles; mergeable sketches for rollups.',
      },
      {
        id: 'V3',
        title: 'The durable, batched sink',
        concept: 'Batch at-least-once into a column store without melting it.',
      },
      {
        id: 'V4',
        title: 'The SSE live fan-out',
        concept: 'SSE fan-out of completed windows without one slow client stalling all.',
      },
    ],
    horizontals: [
      'Protocols: line-protocol ingest + SSE live feeds',
      'Caching: rollup windows as the queryable cache of the firehose',
      'Security: ingest auth, cardinality guards',
      'Observability: ingest rate, sink lag, query latency',
    ],
  },
  {
    id: '06',
    slug: 'object-store',
    title: 'S3-compatible Object Store',
    tagline: '"Store a blob, hand it back by name." It sounds like write(file) / read(file) with an HTTP coat of paint — and for a 10 KB file on a laptop, it is. Every word in S3-compatible object store is a…',
    problem: '"Store a blob, hand it back by name." It sounds like write(file) / read(file) with an HTTP coat of paint — and for a 10 KB file on a laptop, it is. Every word in S3-compatible object store is a trap that only springs at scale. Objects aren\'t 10 KB, they\'re 5 GB, so the instant you write let body = req.bytes() you\'ve put a movie in RAM and one upload OOM-kills the box — you must stream bytes through to disk and never hold more than a chunk (and let a slow disk push backpressure onto a fast client). The same…',
    whatItDoes: [
      'Path-style S3 API: buckets, PUT/GET/DELETE object',
      'Stream uploads/downloads with bounded memory',
      'Multipart upload for large objects',
      'Content-addressed storage with crash-safe commits',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'The content-addressed blob store',
        concept: 'Content-addressed blobs: dedup, integrity, crash-safe commits.',
      },
      {
        id: 'V2',
        title: 'Streaming bodies, end to end',
        concept: 'Stream end-to-end — never buffer a multi-GB body in RAM.',
      },
      {
        id: 'V3',
        title: 'The bucket/key namespace + a crash-safe index',
        concept: 'Flat keyspace, faked folders, GC that doesn\'t delete live data.',
      },
      {
        id: 'V4',
        title: 'Multipart upload + the S3 ETag',
        concept: 'Multipart upload + S3\'s cursed ETag wire compatibility.',
      },
    ],
    horizontals: [
      'Protocols: path-style S3 HTTP API',
      'Caching: optional metadata cache — blobs stay on disk',
      'Security: authn/authz on buckets, no path traversal',
      'Observability: put/get bytes, multipart progress, scrub metrics',
    ],
  },
  {
    id: '07',
    slug: 'distributed-cache',
    title: 'Distributed Cache',
    tagline: 'A single-node cache is a HashMap with an eviction rule. The moment one box can\'t hold the working set — or can\'t survive being rebooted — you need a cluster, and every easy thing gets hard: which…',
    problem: 'A single-node cache is a HashMap with an eviction rule. The moment one box can\'t hold the working set — or can\'t survive being rebooted — you need a cluster, and every easy thing gets hard: which node owns a key, how the set of nodes agrees on who\'s alive without a coordinator, and how you add a node on Black Friday without cold-missing the entire keyspace. This project builds that cluster from the ring up: a hand-rolled LRU/LFU on each node, a consistent-hash ring to shard across them, SWIM gossip so they find…',
    whatItDoes: [
      'PUT /cache/{key} with a body → stores the value (optional ?ttl=<secs>).',
      'GET /cache/{key} → 200 + the bytes, or 404 if absent/expired.',
      'DELETE /cache/{key} → evicts it.',
      'Any node accepts any key: it routes the request to the node(s) that own it and',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'A bounded local cache with O(1) eviction',
        concept: 'Bounded local cache with O(1) LRU/LFU eviction — no cargo-add.',
      },
      {
        id: 'V2',
        title: 'Consistent hashing with virtual nodes',
        concept: 'Consistent hashing with vnodes so resharding doesn\'t cold-miss everything.',
      },
      {
        id: 'V3',
        title: 'Gossip membership & failure detection (SWIM)',
        concept: 'SWIM gossip: membership and failure detection without a coordinator.',
      },
      {
        id: 'V4',
        title: 'Replication & request coordination',
        concept: 'Replication so one node funeral doesn\'t lose a shard.',
      },
    ],
    horizontals: [
      'Protocols: HTTP cache API + inter-node RPC',
      'Caching: the product — local + distributed',
      'Security: cluster membership trust boundaries',
      'Observability: hit ratio, gossip health, ownership moves',
    ],
  },
  {
    id: '08',
    slug: 'message-broker',
    title: 'Mini Message Broker (Kafka-lite)',
    tagline: 'A message broker looks like a queue with extra steps — until you ask it to never lose a committed message, never reorder within a key, hand the same stream to many independent consumers, and do it…',
    problem: 'A message broker looks like a queue with extra steps — until you ask it to never lose a committed message, never reorder within a key, hand the same stream to many independent consumers, and do it while a producer is hammering it at hundreds of MB/s. Kafka\'s answer to all of that is one deceptively simple idea: **an append-only log on disk, split into segments, split into partitions, read by cursor.** This project builds that log from scratch. It\'s Tier 4 because the hard parts — durable sequential writes,…',
    whatItDoes: [
      'Create topics with N partitions',
      'Produce batches of records',
      'Consume by offset / consumer group',
      'Durable offset commits',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Segmented append-only log',
        concept: 'Durable segmented append-only commit log.',
      },
      {
        id: 'V2',
        title: 'Sparse offset index',
        concept: 'Sparse offset index: seek, don\'t scan multi-GB files.',
      },
      {
        id: 'V3',
        title: 'Partitions & the topic',
        concept: 'Partitions trade ordering within a key for parallelism.',
      },
      {
        id: 'V4',
        title: 'Consumer groups & durable offset commits',
        concept: 'Consumer groups + durable offsets = at-least-once delivery.',
      },
    ],
    horizontals: [
      'Protocols: produce/consume HTTP (or binary) APIs',
      'Caching: page cache friendliness of sequential logs',
      'Security: topic ACLs basics',
      'Observability: produce throughput, consumer lag, disk usage',
    ],
  },
  {
    id: '09',
    slug: 'raft-kv',
    title: 'Distributed KV Store with Raft',
    tagline: 'A replicated key-value store sounds like "a HashMap, but on three machines." The trap is the word replicated: the moment more than one node can accept a write, you have to answer "which write…',
    problem: 'A replicated key-value store sounds like "a HashMap, but on three machines." The trap is the word replicated: the moment more than one node can accept a write, you have to answer "which write won?", "what if a node was offline for it?", and "what if two nodes both think they\'re in charge?" — while machines crash and the network drops, delays, and reorders messages at will. Raft is one carefully-proven answer: elect a single leader, funnel every write through its append-only log, and only call an entry committed…',
    whatItDoes: [
      'PUT /kv/{key} {value} → a replicated write; succeeds only via the leader.',
      'GET /kv/{key} → a linearizable read (served by a confirmed leader).',
      'DELETE /kv/{key} → a replicated delete.',
      'GET /status → this node\'s role, term, leader, and commit/apply progress.',
      'Internal /raft/* endpoints carry RequestVote / AppendEntries /',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Leader election',
        concept: 'Leader election: one leader per term, or none.',
      },
      {
        id: 'V2',
        title: 'Log replication & commit',
        concept: 'A majority must have the entry before it is committed.',
      },
      {
        id: 'V3',
        title: 'The replicated state machine + linearizable reads',
        concept: 'The log becomes state; reads must be linearizable.',
      },
      {
        id: 'V4',
        title: 'Snapshots & log compaction',
        concept: 'Snapshots keep the log from growing forever.',
      },
    ],
    horizontals: [
      'Protocols: KV HTTP + Raft RPCs between nodes',
      'Caching: applied state machine in memory',
      'Security: cluster membership / untrusted peers',
      'Observability: term, role, commit/apply index, election count',
    ],
  },
  {
    id: '10',
    slug: 'api-gateway',
    title: 'API Gateway / L7 Reverse Proxy',
    tagline: '"Just forward the request" is a lie you tell yourself until the first 2 GiB upload buffers into RAM, a client smuggles a Transfer-Encoding header past you to the backend, one slow upstream drags…',
    problem: '"Just forward the request" is a lie you tell yourself until the first 2 GiB upload buffers into RAM, a client smuggles a Transfer-Encoding header past you to the backend, one slow upstream drags every request to its timeout, and a dead node turns a single failure into a full outage. An API gateway sits in front of a fleet and has to do the hard parts for everyone: route by path/host, spread load across a pool, notice a backend is dying and stop sending it traffic, terminate TLS, and stay a thin, streaming,…',
    whatItDoes: [
      'Listen on one port; proxy to upstreams by route',
      'Load-balance across a healthy backend pool',
      'Fail fast with circuit breaking when upstreams die',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'The reverse-proxy forwarding core',
        concept: 'Stream bytes through — don\'t buffer whole bodies in the proxy.',
      },
      {
        id: 'V2',
        title: 'The request routing engine',
        concept: 'Match routes efficiently; don\'t scan every rule per request.',
      },
      {
        id: 'V3',
        title: 'Load balancing across a backend pool',
        concept: 'Load balance beyond naive round-robin (health, least-conn, …).',
      },
      {
        id: 'V4',
        title: 'Health checking & circuit breaking',
        concept: 'Health checks + circuit breakers stop cascading failures.',
      },
    ],
    horizontals: [
      'Protocols: HTTP/1.1 proxying, hop-by-hop header hygiene',
      'Caching: optional response cache — not the core',
      'Security: header smuggling defenses, TLS/mTLS stretch',
      'Observability: upstream latency, circuit state, error rates',
    ],
  },
  {
    id: '11',
    slug: 'vod-streaming',
    title: 'VOD Streaming Server (HLS/DASH)',
    tagline: '"Serve a video file over HTTP." A GET that returns an .mp4 is that — and it works right up until a real player, a real network, or a real seek touches it. A progressive MP4 puts its index (moov)…',
    problem: '"Serve a video file over HTTP." A GET that returns an .mp4 is that — and it works right up until a real player, a real network, or a real seek touches it. A progressive MP4 puts its index (moov) and its media (mdat) in two big blobs, so a player can\'t start until it has fetched enough of the file, can\'t seek without a round trip to re-read the index, and can\'t switch quality without throwing the whole download away. The web\'s answer — the thing YouTube, Netflix and every <video> tag actually do — is to not ship…',
    whatItDoes: [
      'Serve HLS/DASH from a media library on disk',
      'Generate manifests and fMP4 segments',
      'Byte-range requests for seeking',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'ISO-BMFF demuxer',
        concept: 'Read ISO-BMFF boxes by hand — moov/mdat and friends.',
      },
      {
        id: 'V2',
        title: 'The fMP4 / CMAF segmenter',
        concept: 'Write fMP4/CMAF segments by hand for adaptive streaming.',
      },
      {
        id: 'V3',
        title: 'Manifest generation',
        concept: 'HLS .m3u8 and DASH .mpd manifests that real players accept.',
      },
      {
        id: 'V4',
        title: 'Byte-range delivery + the ABR ladder',
        concept: 'Byte-range delivery and ABR ladder selection over HTTP.',
      },
    ],
    horizontals: [
      'Protocols: HLS/DASH over HTTP with correct MIME/ranges',
      'Caching: CDN-friendly segment immutability',
      'Security: path safety on the media library',
      'Observability: segment serve latency, 206 rates',
    ],
  },
  {
    id: '12',
    slug: 'transcode-pipeline',
    title: 'Distributed Transcoding Pipeline',
    tagline: 'Project 11 turned one video file into a playable HLS/DASH stream — but it assumed the file was already at the right codec and bitrates. Real platforms don\'t get that gift: an upload arrives as one…',
    problem: 'Project 11 turned one video file into a playable HLS/DASH stream — but it assumed the file was already at the right codec and bitrates. Real platforms don\'t get that gift: an upload arrives as one giant 4K ProRes (or a phone\'s H.265), and before it can be packaged it has to be transcoded into the whole ladder (1080p, 720p, 480p, …). Transcoding a two-hour movie serially on one machine takes hours — longer than the movie. The only way out is to go wide: cut the source into chunks, transcode the chunks in…',
    whatItDoes: [
      'POST /jobs with source + ABR ladder',
      'Chunk, schedule, transcode in parallel',
      'Stitch outputs into a playable package',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Keyframe-aligned chunking',
        concept: 'Cut at keyframes so chunks encode independently.',
      },
      {
        id: 'V2',
        title: 'The job DAG + scheduler',
        concept: 'Model work as a DAG; schedule dependencies correctly.',
      },
      {
        id: 'V3',
        title: 'Parallel transcode workers',
        concept: 'Idempotent parallel workers — retries must not corrupt output.',
      },
      {
        id: 'V4',
        title: 'Stitch + remux',
        concept: 'Stitch and remux so the seam is invisible.',
      },
    ],
    horizontals: [
      'Protocols: job API + worker claim protocol',
      'Caching: chunk output reuse on retry',
      'Security: source path allowlists',
      'Observability: DAG stage latency, straggler detection',
    ],
    boss: {
      name: 'The Straggler',
      idea: 'One slow chunk must not freeze the whole ladder — finish under a deadline with straggler handling.',
    },
  },
  {
    id: '13',
    slug: 'live-ingest',
    title: 'Live Ingest Server (RTMP → LL-HLS)',
    tagline: 'Project 11 packaged a file that already existed; project 12 produced that file from an upload. This one has no file — the media is arriving, right now, from a camera. A broadcaster (OBS, ffmpeg, a…',
    problem: 'Project 11 packaged a file that already existed; project 12 produced that file from an upload. This one has no file — the media is arriving, right now, from a camera. A broadcaster (OBS, ffmpeg, a phone) opens a socket and starts pushing H.264 + AAC over RTMP, and thousands of viewers want to watch **within a few seconds of real life**. That "few seconds" is the whole game: regular HLS cuts 6-second segments and makes a player buffer three of them, so the viewer is ~15–30 seconds behind the glass. Low-Latency…',
    whatItDoes: [
      'Accept RTMP publish from OBS/ffmpeg',
      'Repackage to live fMP4 / LL-HLS',
      'Serve playlists and segments to players',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'RTMP handshake + chunk-stream reader',
        concept: 'Parse RTMP handshake and chunk streams by hand.',
      },
      {
        id: 'V2',
        title: 'AMF0 commands + the publish state machine',
        concept: 'AMF0 commands drive the publish state machine.',
      },
      {
        id: 'V3',
        title: 'Live fMP4 repackaging',
        concept: 'Rewrap H.264/AAC into live fMP4 without re-encoding.',
      },
      {
        id: 'V4',
        title: 'Low-Latency HLS playlist + blocking delivery',
        concept: 'LL-HLS playlists + blocking delivery to break the latency wall.',
      },
    ],
    horizontals: [
      'Protocols: RTMP ingest + LL-HLS delivery',
      'Caching: playlist/segment hot path',
      'Security: stream keys',
      'Observability: glass-to-glass latency, ingest bitrate',
    ],
    boss: {
      name: 'The Latency Wall',
      idea: 'Glass-to-glass live latency under a real publisher — prove LL-HLS actually breaks the wall.',
    },
  },
  {
    id: '14',
    slug: 'media-transport',
    title: 'Real-time Media Transport (RTP/RTCP)',
    tagline: 'Projects 11–13 always had HTTP to hide behind: TCP guaranteed the bytes arrived, in order, eventually, and a player could buffer its way over any bump. This one takes that safety net away.…',
    problem: 'Projects 11–13 always had HTTP to hide behind: TCP guaranteed the bytes arrived, in order, eventually, and a player could buffer its way over any bump. This one takes that safety net away. Real-time media — a video call, a game stream, an esports feed — cannot wait for TCP\'s retransmit-and-reorder, because a packet that arrives 400 ms late is useless: the moment it was meant to be shown has already passed. So real-time media runs on UDP, which gives you exactly nothing — packets are dropped, duplicated,…',
    whatItDoes: [
      'UDP RTP sender/receiver roles',
      'Jitter buffer for smooth playout',
      'RTCP feedback + NACK retransmission',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'RTP packetization + depacketization',
        concept: 'Packetize frames into RTP datagrams and rebuild them.',
      },
      {
        id: 'V2',
        title: 'Jitter buffer',
        concept: 'Jitter buffer: smooth playout from jittery arrivals.',
      },
      {
        id: 'V3',
        title: 'RTCP + selective retransmission (NACK)',
        concept: 'RTCP + NACK recover the losses that still matter.',
      },
      {
        id: 'V4',
        title: 'Congestion control',
        concept: 'Congestion control paces to the bandwidth the path actually has.',
      },
    ],
    horizontals: [
      'Protocols: RTP/RTCP over UDP',
      'Caching: jitter buffer as playout cache',
      'Security: unauthenticated UDP reality — auth stretch',
      'Observability: loss, RTT, bitrate, NACK rate',
    ],
    boss: {
      name: 'The Lossy Mile',
      idea: 'Hold watchable video under packet loss with NACK + congestion control — not just the happy path.',
    },
  },
  {
    id: '15',
    slug: 'webrtc-sfu',
    title: 'WebRTC SFU (Selective Forwarding Unit)',
    tagline: 'Project 14 gave you the hard-real-time toolkit for one stream over lossy UDP — RTP, jitter buffers, NACK, congestion control. This project asks the question that turns a transport into a…',
    problem: 'Project 14 gave you the hard-real-time toolkit for one stream over lossy UDP — RTP, jitter buffers, NACK, congestion control. This project asks the question that turns a transport into a conferencing system: how does one publisher reach many viewers, each on a different link, without the whole thing collapsing? The two obvious answers both fail. A mesh (everyone sends everyone a copy) makes each publisher upload N−1 streams — the uplink dies at ~4 people. An MCU (a server that decodes everyone, composites one…',
    whatItDoes: [
      'Browser joins a room via signaling',
      'SFU forwards RTP without re-encoding',
      'Simulcast layer selection per subscriber',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'ICE / STUN connectivity',
        concept: 'ICE/STUN so a browser behind NAT can reach you.',
      },
      {
        id: 'V2',
        title: 'Selective RTP forwarding',
        concept: 'Forward encoded RTP unchanged — one upload, many tailored downloads.',
      },
      {
        id: 'V3',
        title: 'Simulcast layer selection',
        concept: 'Simulcast: pick the layer each subscriber\'s link can take.',
      },
      {
        id: 'V4',
        title: 'Bandwidth estimation',
        concept: 'Estimate each subscriber\'s available bandwidth.',
      },
    ],
    horizontals: [
      'Protocols: WebRTC ICE/DTLS/SRTP (as SPEC scopes)',
      'Caching: forwarding tables / layer selection state',
      'Security: room auth, TURN credentials stretch',
      'Observability: per-subscriber bitrate, loss, CPU',
    ],
    boss: {
      name: 'The Crowded Room',
      idea: 'One publisher, many subscribers — SFU stays up as the room fills without melting CPU or uplink.',
    },
  },
  {
    id: '16',
    slug: 'live-platform',
    title: 'Live Streaming Platform (Twitch-lite)',
    tagline: 'This is the capstone — the marquee project. There\'s no new primitive to invent here; the hard part is integration: wiring the pieces you already built (chat fan-out from 03, HLS packaging from 11,…',
    problem: 'This is the capstone — the marquee project. There\'s no new primitive to invent here; the hard part is integration: wiring the pieces you already built (chat fan-out from 03, HLS packaging from 11, transcode from 12, RTMP ingest from 13) into one glass-to-glass pipeline that survives a real crowd. A broadcaster\'s frame is captured, ingested, transcoded into an ABR ladder, packaged as low-latency HLS, delivered through an edge, and painted on a thousand viewers\' screens — while chat scrolls in real time. Then a…',
    whatItDoes: [
      'Ingest start/stop control plane',
      'Transcode ladder + LL-HLS delivery',
      'Live chat alongside the stream',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Stream control plane / session lifecycle',
        concept: 'Orchestrate stream session lifecycle end-to-end.',
      },
      {
        id: 'V2',
        title: 'Autoscaling transcode worker pool',
        concept: 'Autoscale transcode workers from queue signal and drain correctly.',
      },
      {
        id: 'V3',
        title: 'LL-HLS edge delivery with request coalescing',
        concept: 'Edge delivery with request coalescing shields the origin.',
      },
      {
        id: 'V4',
        title: 'Chat & presence fan-out at scale',
        concept: 'Chat/presence must survive a viral hot channel.',
      },
    ],
    horizontals: [
      'Protocols: control plane webhooks + LL-HLS + chat WS',
      'Caching: edge coalescing / playlist cache',
      'Security: stream keys, chat abuse basics',
      'Observability: viewers, transcode lag, chat fan-out',
    ],
    boss: {
      name: 'The Viral Spike',
      idea: '10 → 100k viewers: autoscale, edge coalescing, and chat must hold when a streamer goes viral.',
    },
  },
  {
    id: '17',
    slug: 'global-conferencing',
    title: 'Global WebRTC Conferencing (cascaded SFU)',
    tagline: 'Project 15 gave you one SFU: a server that forwards a publisher\'s encoded RTP to many subscribers in the same room, no transcode, one upload → one tailored download per viewer. It makes a…',
    problem: 'Project 15 gave you one SFU: a server that forwards a publisher\'s encoded RTP to many subscribers in the same room, no transcode, one upload → one tailored download per viewer. It makes a 50-person call in one data centre possible. This capstone asks the question that turns a regional SFU into a global conferencing system: what happens when the 50 people are in Tokyo, Frankfurt and São Paulo at once? The naive answer — point everyone at a single SFU — fails twice. First on latency: a Tokyo viewer watching a…',
    whatItDoes: [
      'One process = one regional SFU',
      'Participants connect to nearest region',
      'Cascade media between regions',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Global room placement via consensus',
        concept: 'Consensus picks one home region for a room — once.',
      },
      {
        id: 'V2',
        title: 'Inter-SFU cascade transport',
        concept: 'Cascade: forward once between regions, fan out locally.',
      },
      {
        id: 'V3',
        title: 'Cross-region simulcast routing',
        concept: 'Carry the union of simulcast demand across regions, no more.',
      },
      {
        id: 'V4',
        title: 'Server-side recording',
        concept: 'Recording is just another subscriber on the cascade.',
      },
    ],
    horizontals: [
      'Protocols: inter-SFU cascade + client WebRTC',
      'Caching: regional subscription state',
      'Security: cross-region trust',
      'Observability: hairpin avoidance, cascade bitrate, region RTT',
    ],
    boss: {
      name: 'The Hairpin',
      idea: 'Tokyo viewer watching Tokyo publisher must not hairpin through Frankfurt — prove cascade locality.',
    },
  },
  {
    id: '18',
    slug: 'ledger-payments-core',
    title: 'Ledger / Payments Core (Stripe-lite)',
    tagline: 'Moving money is the one place in backend where a race condition has a dollar figure attached. A ledger looks like CRUD — insert a row, read a balance — until two requests touch the same account at…',
    problem: 'Moving money is the one place in backend where a race condition has a dollar figure attached. A ledger looks like CRUD — insert a row, read a balance — until two requests touch the same account at the same instant and one of them quietly spends money that isn\'t there. This project is where **isolation levels stop being interview trivia**: the same code is correct under SERIALIZABLE and silently wrong under READ COMMITTED, and the only way to know is to build the invariant and then attack it with concurrency. On…',
    whatItDoes: [
      'Create accounts; transfer with Idempotency-Key',
      'Double-entry postings; query balances',
      'Signed webhooks on settlement',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Double-entry ledger core',
        concept: 'Append-only double-entry postings that never lose a cent.',
      },
      {
        id: 'V2',
        title: 'The balance invariant under concurrency',
        concept: 'Isolation levels: correct under SERIALIZABLE, wrong under READ COMMITTED.',
      },
      {
        id: 'V3',
        title: 'Idempotency keys',
        concept: 'Idempotency keys: exactly-once effects over at-least-once networks.',
      },
      {
        id: 'V4',
        title: 'Signed webhooks with retries',
        concept: 'Signed webhooks with retries so the outside world hears once.',
      },
    ],
    horizontals: [
      'Protocols: transfers API + signed webhooks',
      'Caching: balance reads must respect isolation — not a free cache',
      'Security: idempotency keys, webhook signatures',
      'Observability: postings/sec, conflict retries, webhook lag',
    ],
    boss: {
      name: 'The Double Spend',
      idea: 'Concurrent transfers cannot violate balances — attack isolation until the invariant holds.',
    },
  },
  {
    id: '19',
    slug: 'bittorrent',
    title: 'BitTorrent Client + Seeder',
    tagline: 'Downloading a file over HTTP is trivial: one server, one connection, done. BitTorrent throws all of that away. There is no server — you assemble the file out of blocks handed to you by anonymous…',
    problem: 'Downloading a file over HTTP is trivial: one server, one connection, done. BitTorrent throws all of that away. There is no server — you assemble the file out of blocks handed to you by anonymous strangers, any of whom may be slow, hostile, or lying. You find those strangers with no central directory you can trust, verify every byte before you believe it, and then turn around and share what you have so the swarm scales up with demand instead of collapsing. It\'s the purest protocol workout on the board: raw TCP,…',
    whatItDoes: [
      'Read a .torrent file (or a magnet: link) and compute its infohash.',
      'Announce to the tracker, get a list of peers.',
      'Connect to peers, handshake, download pieces, verify each against the SHA-1 in',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Bencode',
        concept: 'Bencode: the wire\'s data format.',
      },
      {
        id: 'V2',
        title: 'Metainfo & the infohash',
        concept: 'Infohash identity without a central registry.',
      },
      {
        id: 'V3',
        title: 'Tracker announce',
        concept: 'Tracker announce over HTTP and UDP.',
      },
      {
        id: 'V4',
        title: 'Peer wire protocol',
        concept: 'Peer wire protocol — raw TCP conversation.',
      },
      {
        id: 'V5',
        title: 'Piece selection & verification',
        concept: 'Piece selection, rarest-first, verify every byte.',
      },
      {
        id: 'V6',
        title: 'The seeder',
        concept: 'Seed fairly under load so the swarm scales up with demand.',
      },
    ],
    horizontals: [
      'Protocols: bencode, tracker HTTP/UDP, peer wire',
      'Caching: piece/bitfield local state',
      'Security: verify every piece hash — trust nothing',
      'Observability: swarm peers, download rate, choke state',
    ],
    boss: {
      name: 'The Flash Crowd',
      idea: 'Swarm joins explode; seeder stays fair and the download completes with verified pieces.',
    },
  },
  {
    id: '20',
    slug: 'full-text-search',
    title: 'Full-Text Search Engine (Elasticsearch-lite)',
    tagline: 'WHERE text LIKE \'%rust%\' works until it doesn\'t: it scans every row, can\'t rank, and ignores that "Running" should match "run". A search engine is a different data structure entirely — an inverted…',
    problem: 'WHERE text LIKE \'%rust%\' works until it doesn\'t: it scans every row, can\'t rank, and ignores that "Running" should match "run". A search engine is a different data structure entirely — an inverted index that maps each word to the documents containing it, so a query is a dictionary lookup and a list walk instead of a scan. Elasticsearch/Lucene wrap that core in analysis, BM25 relevance, immutable segments, background merging, and sharded fan-out — and every one of those exists to keep search *fast and relevant…',
    whatItDoes: [
      'POST /documents {id?, text} → index a document; returns its (shard, doc_id).',
      'POST /_bulk (NDJSON, one document per line) → index a batch.',
      'POST /_refresh → flush buffered documents into segments so they become searchable.',
      'GET /search?q=&size= → the top-size documents for a query, ranked by relevance,',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'The analyzer',
        concept: 'Analyzer: text → terms, symmetrically at index and query time.',
      },
      {
        id: 'V2',
        title: 'The inverted index & on-disk segments',
        concept: 'Inverted index on disk via mmap — don\'t scan rows.',
      },
      {
        id: 'V3',
        title: 'BM25 ranking',
        concept: 'BM25 ranks relevance, not just boolean match.',
      },
      {
        id: 'V4',
        title: 'Segment merging & deletes',
        concept: 'Segment merge + deletes keep search fast as the corpus grows.',
      },
      {
        id: 'V5',
        title: 'Scatter-gather across shards',
        concept: 'Scatter-gather across shards and merge ranked lists.',
      },
    ],
    horizontals: [
      'Protocols: index/search HTTP API',
      'Caching: query cache stretch',
      'Security: input size limits',
      'Observability: query latency, segment count, merge debt',
    ],
    boss: {
      name: 'The Long Tail',
      idea: 'Rare terms and large corpora — search stays fast and relevant without scanning the world.',
    },
  },
  {
    id: '21',
    slug: 'workflow-engine',
    title: 'Workflow Engine (Temporal-lite)',
    tagline: 'A workflow engine sells one promise: durable execution. You write a normal-looking function — "charge the card, wait 3 days, if not cancelled ship the order, email the customer" — and the engine…',
    problem: 'A workflow engine sells one promise: durable execution. You write a normal-looking function — "charge the card, wait 3 days, if not cancelled ship the order, email the customer" — and the engine guarantees it runs *to completion, exactly as written, even though the process running it will crash, deploy, and restart many times before it finishes.* That is a wild promise, and the only way to keep it is to stop storing the program\'s state and start storing its history: an append-only log of everything that…',
    whatItDoes: [
      'StartWorkflow / Signal / Query style APIs',
      'Workers long-poll a task queue',
      'Timers survive process restart',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'Event-sourced history log',
        concept: 'Event-sourced history: the state IS the log.',
      },
      {
        id: 'V2',
        title: 'Deterministic replay',
        concept: 'Deterministic replay rebuilds state identically after a crash.',
      },
      {
        id: 'V3',
        title: 'Durable timers',
        concept: 'Durable timers outlive the process — no tokio::sleep as truth.',
      },
      {
        id: 'V4',
        title: 'gRPC worker dispatch',
        concept: 'Long-poll worker dispatch with at-least-once task delivery.',
      },
      {
        id: 'V5',
        title: 'Sticky workflow-state cache',
        concept: 'Sticky cache skips replay you don\'t need — carefully.',
      },
    ],
    horizontals: [
      'Protocols: gRPC worker poll + workflow start APIs',
      'Caching: sticky workflow-state cache',
      'Security: task-queue isolation',
      'Observability: history length, replay time, timer lag',
    ],
    boss: {
      name: 'The Reaper',
      idea: 'Kill workers mid-workflow — another worker must replay history and continue correctly.',
    },
  },
  {
    id: '22',
    slug: 'lsm-redis',
    title: 'LSM Storage Engine + Redis-Compatible Server',
    tagline: 'This is the keystone. Every earlier project reached for a store — the message broker (#08) an append-only log, the Raft KV (#09) a state machine, the distributed cache (#07) a map behind a…',
    problem: 'This is the keystone. Every earlier project reached for a store — the message broker (#08) an append-only log, the Raft KV (#09) a state machine, the distributed cache (#07) a map behind a protocol real clients speak. Here you build the store itself, from the WAL up, and put a RESP front-end on it so redis-cli and redis-benchmark connect with no adapter. An LSM engine is what powers RocksDB, LevelDB, Cassandra, and the storage layer under half the databases you\'ve used. It is "just a key/value store" the way a…',
    whatItDoes: [
      'Speaks RESP on :6379, so redis-cli connects with no arguments.',
      'SET key value, GET key, DEL key …, EXISTS, PING, AUTH — a real subset of',
    ],
    verticals: [
      {
        id: 'V1',
        title: 'RESP protocol codec',
        concept: 'RESP codec so redis-cli connects with no adapter.',
      },
      {
        id: 'V2',
        title: 'Write-ahead log',
        concept: 'WAL: durability before the acknowledgement.',
      },
      {
        id: 'V3',
        title: 'Memtable',
        concept: 'Memtable: sorted in-memory write buffer.',
      },
      {
        id: 'V4',
        title: 'SSTable',
        concept: 'SSTable: immutable sorted on-disk file.',
      },
      {
        id: 'V5',
        title: 'Bloom filters',
        concept: 'Bloom filters skip files that can\'t hold the key.',
      },
      {
        id: 'V6',
        title: 'Compaction',
        concept: 'Compaction keeps write debt from stalling the engine.',
      },
      {
        id: 'V7',
        title: 'Block cache',
        concept: 'Hand-built LRU block cache over decoded SSTable blocks.',
      },
    ],
    horizontals: [
      'Protocols: RESP on :6379',
      'Caching: block cache over SSTable blocks',
      'Security: AUTH',
      'Observability: write stall, compaction debt, hit ratio',
    ],
    boss: {
      name: 'The Write Stall',
      idea: 'Sustained writes without compaction debt turning into a write stall — prove the LSM stays healthy.',
    },
  },
]

const byId = new Map(projectDetails.map((p) => [p.id, p]))

export function getProject(id: string): ProjectDetail | undefined {
  return byId.get(id)
}

export function projectFolder(p: ProjectDetail): string {
  return `projects/${p.id}-${p.slug}`
}

export function projectLinks(p: ProjectDetail) {
  const folder = projectFolder(p)
  return {
    spec: `${REPO}/blob/master/${folder}/SPEC.md`,
    code: `${REPO}/tree/master/${folder}`,
  }
}
