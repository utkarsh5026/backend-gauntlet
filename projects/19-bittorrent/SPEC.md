<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 19 — BitTorrent Client + Seeder

> Downloading a file over HTTP is trivial: one server, one connection, done. BitTorrent
> throws all of that away. There is **no server** — you assemble the file out of blocks
> handed to you by anonymous strangers, any of whom may be slow, hostile, or lying. You
> find those strangers with **no central directory** you can trust, verify **every byte**
> before you believe it, and then turn around and **share** what you have so the swarm
> scales *up* with demand instead of collapsing. It's the purest protocol workout on the
> board: raw TCP, raw UDP, binary framing, content-addressed identity, and a fairness
> algorithm — no framework to hide behind.

## What it does (the easy part)
- Read a `.torrent` file (or a `magnet:` link) and compute its **infohash**.
- **Announce** to the tracker, get a list of peers.
- **Connect** to peers, handshake, download pieces, **verify** each against the SHA-1 in
  the metainfo, and write the file.
- **Seed**: serve pieces to other peers, fairly, without falling over under a swarm.
- A thin HTTP control plane to drive it: `POST /torrents` (a `.torrent` body) and
  `POST /torrents/magnet`, `GET /torrents` for progress, plus `/healthz` and `/metrics`.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips to
> ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Bencode — the wire's data format
Everything in BitTorrent is **bencoded**: the `.torrent`, the tracker's reply, the DHT.
Four types — integers `i42e`, byte-strings `4:spam`, lists `l…e`, dicts `d…e` (keys
sorted as raw bytes). Build the codec in `src/bencode.rs`. The twist that makes this a
*challenge*: to compute the infohash (V2) you must SHA-1 the **exact original bytes** of
the `info` dict, so your decoder has to surface a value's precise byte span and your
encoder has to be **canonical** (sorted keys, no leading zeros, no whitespace) — else a
round-trip changes the bytes and every infohash you produce is wrong.

**Done when ALL true:**
- [ ] Decode handles all four types incl. arbitrary nesting, and **rejects malformed input** (leading zeros `i03e`, negative zero `i-0e`, truncation, a length that overruns, duplicate/unsorted dict keys, trailing junk) with an `Err` — never a panic.
- [ ] Encode is **canonical**: dict keys sorted as raw byte strings, integers with no leading zeros, no extra bytes.
- [ ] **Byte-exact round-trip**: for every value in the corpus, `encode(decode(x)) == x`.
- [ ] Byte-strings are **binary-safe** — they may hold non-UTF-8 bytes (piece hashes are raw SHA-1), so keys/strings are bytes, not `String`.
- [ ] You can recover the **exact byte range** of a nested value (the `info` dict) without re-encoding it.

**Proof:** unit tests over a checked-in real `.torrent` corpus + a property test
`prop_bencode_roundtrips`; a test that every malformed input returns `Err` rather than
panicking.

*Concept to internalize:* canonical serialization, and why a content hash is a hash of
*bytes*, not of a parsed structure — the #1 source of "why doesn't my infohash match?"

### V2. Metainfo & the infohash — identity without a registry
Parse a `.torrent` into a typed `Metainfo` and compute the **infohash = SHA-1(exact
`info` bytes)** — the 20-byte name every peer and tracker uses for this content. There
is no registry; two clients agree they mean the same file because they independently
hashed the same bytes. Also parse `magnet:?xt=urn:btih:…` links (infohash + trackers,
but no metainfo — that comes from peers later). Build it in `src/metainfo.rs`.

**Done when ALL true:**
- [ ] A valid `.torrent` parses into typed fields: piece length, the piece-hash table (each exactly 20 bytes), and the file list (single- **and** multi-file).
- [ ] **Infohash = SHA-1 of the exact `info` bytes**, and it **matches** the value a real client/tracker reports for the same file (cross-check against one known torrent).
- [ ] Flipping one byte of the `info` dict yields a **different** infohash (identity is byte-sensitive).
- [ ] `magnet:` links parse: infohash as **hex or base32**, every `tr=` tracker, the optional `dn=` name.
- [ ] Parse is **consistency-checked**: `piece_count == ceil(total_length / piece_length)` and `total_length == Σ file lengths`; a doctored torrent is rejected, not trusted.

**Proof:** a test asserting the checked-in torrent's `info_hash.to_hex()` equals the
known value; a hex-vs-base32 magnet test resolving to the same infohash; a
consistency-rejection test.

*Concept to internalize:* content-addressing / identity derived from bytes, and
single-file vs multi-file layout.

### V3. Tracker announce — peer discovery over HTTP *and* UDP
Ask a tracker "who has infohash X?" and report your progress, over **both** transports.
Build it in `src/tracker.rs`. HTTP (BEP 3): a `GET /announce` whose bencoded reply
carries a **compact** peer list (6 bytes/peer). UDP (BEP 15): a binary protocol where
you first `connect` for a short-lived connection-id (cheap anti-spoofing), then
`announce`, everything big-endian and paired by transaction-id. Announces are a periodic
*side effect* — `started` on join, `stopped` on exit, and honor the `interval`.

**Done when ALL true:**
- [ ] The HTTP announce query is correct — in particular `info_hash` and `peer_id` are **percent-encoded byte-for-byte** from their raw 20 bytes (not treated as text) — and the bencoded reply (incl. a `failure reason`) parses.
- [ ] The **compact peer list** (6 bytes = 4-byte IPv4 + 2-byte big-endian port) decodes to peer addresses.
- [ ] The **UDP** tracker works: connect → announce with correct transaction/connection ids and big-endian framing; an expired connection-id is re-established.
- [ ] `started` is sent on join and `stopped` on graceful exit; the returned `interval` is honored (no hammering).
- [ ] A tracker timeout/error is handled (retry/backoff or fall through to another tracker) — **one dead tracker doesn't sink the download**.

**Proof:** an integration test (needs `docker compose up`) that announces to the compose
tracker and gets the reference peer back; unit tests for compact-peer decode and the UDP
connect/announce frames (hand-built bytes, no network). **Stretch:** trackerless
discovery via a Kademlia **DHT** (BEP 5) and Local Peer Discovery.

*Concept to internalize:* why compact encoding exists at swarm scale, and the UDP
connection-id handshake as spoofing defense.

### V4. Peer wire protocol — the raw-TCP conversation
Over a raw TCP socket in `src/peer.rs`: the fixed **68-byte handshake**
(`<19>"BitTorrent protocol"<8 reserved><infohash><peer_id>`), then a stream of
**length-prefixed messages** (`choke/unchoke/interested/not-interested/have/bitfield/
request/piece/cancel`; a zero-length frame is keep-alive). Track the four-flag
choke/interest state and the peer's bitfield.

**Done when ALL true:**
- [ ] The handshake is sent and **validated**: a peer whose infohash ≠ the one you dialed is dropped before any message is exchanged.
- [ ] Messages **frame correctly off a raw stream**: a message split across reads reassembles, and two messages arriving in one read both parse — driven by the length prefix, keep-alive handled.
- [ ] The **four state flags** and the peer bitfield update on every relevant message; `interested`/`unchoke` are driven by that state, not sent blindly.
- [ ] A malformed or **oversized** message (declared length beyond a sane cap) is rejected/closed — **no unbounded allocation** from a hostile peer.
- [ ] `request`/`piece` blocks (length ≤ 16 KiB) encode and decode correctly.

**Proof:** a loopback test where two of your own state machines handshake and exchange
every message type; a **framing test** that feeds the bytes one at a time and gets the
same messages back; an oversized-length test that rejects without a big allocation.
**Bonus:** a real handshake + bitfield exchange against the `transmission` reference peer.

*Concept to internalize:* turning a byte stream into messages (length-prefix +
`read_exact`), the choke/interest state machine, and "never trust a peer" as a coding
discipline (bound before you allocate).

### V5. Piece selection & verification — assembling from strangers
Drive the leech loop in `src/download.rs`: pick a piece (**rarest-first** from peers'
bitfields, not sequential), split it into ≤ 16 KiB block `request`s pipelined across
unchoked peers, reassemble, and **verify the piece's SHA-1 against the metainfo hash
before writing it**. A piece that fails is discarded and refetched — a lying peer cannot
corrupt your file. Announce `have` as pieces complete; switch to **endgame** for the tail.

**Done when ALL true:**
- [ ] The downloaded file is **bit-identical** to the original — every piece verified against its SHA-1 before it counts as `have`.
- [ ] A block that **fails verification is discarded and refetched**; a lying/corrupting peer cannot poison the output.
- [ ] Piece selection is **rarest-first** (driven by peers' bitfields), demonstrable from logs/metrics — not naive sequential.
- [ ] Requests are **pipelined** (several in-flight blocks per peer) — throughput isn't one-block-per-round-trip.
- [ ] `have` is announced as pieces complete, and the last pieces use **endgame** so one slow peer can't stall the finish.
- [ ] **Resume**: on restart, already-verified pieces on disk are recognized, not refetched.

**Proof:** an end-to-end test that leeches a small torrent from the reference seed and
asserts the output's SHA-256 == the source's; a fault-injection test feeding a corrupted
block and asserting the piece still completes correctly; `docs/19-design.md` names the
selection strategy.

*Concept to internalize:* why rarest-first keeps a swarm alive, verify-before-write as
the trust boundary, and endgame as a latency/bandwidth trade.

### V6. The seeder — serving pieces fairly under load
The upload half, in `src/seeder.rs`: accept inbound peers, answer `request`s with
`piece` data from the verified store, and run the **choke algorithm** — a fixed number
of **upload slots** (regular unchokes, refreshed ~every 10 s) plus one **optimistic
unchoke** (~every 30 s) to probe newcomers, and a cap on total connections. You *can't*
unchoke everyone; bounded, deliberate scheduling is what lets one seed survive a swarm.

**Done when ALL true:**
- [ ] Accepts inbound peers (handshake + bitfield) and answers valid `request`s with the **correct** `piece` bytes read from the store.
- [ ] **Upload slots are capped**: at most `K` peers unchoked at once, regardless of how many connect — verifiable in logs/metrics.
- [ ] An **optimistic unchoke** rotates a slot to a random choked peer periodically (new peers get a chance).
- [ ] A request for a piece you don't have, or an out-of-range/oversized one, is **refused** — not served, not a panic.
- [ ] Per-peer memory is **bounded** (blocks streamed from disk — no whole-file-per-peer buffering); total connections are capped.
- [ ] On graceful shutdown the seeder stops accepting, finishes in-flight sends, and the client announces `stopped` to the tracker.

**Proof:** a test where `N > K` leechers connect and at most `K` (+1 optimistic) are
unchoked at any instant; a leecher completing a full, SHA-1-verified download served
**purely by your seeder**; the boss-fight bench below.

*Concept to internalize:* why finite upload bandwidth forces a scheduler, upload slots +
optimistic unchoke as a fair bounded policy, and backpressure on the upload path.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.
These are the cross-cutting concerns, distinct from the per-vertical criteria above.

### Protocols
- [ ] **Forward-compatibility:** unknown peer message ids and reserved handshake bits are **ignored, not fatal** (a peer using an extension you don't implement stays connected). *(Proof: a test injecting an unknown id mid-stream; the session survives.)*
- [ ] Both a `.torrent` **and** a `magnet:` link can start a download through the control plane — not just one. *(Proof: control-plane tests for both `POST /torrents` and `POST /torrents/magnet`.)*
- [ ] A well-formed **`peer_id`** (client prefix + random, stable per run) is sent in the handshake and announce. *(Proof: `peer_id` format test.)*

### Security / trust
- [ ] **Path-traversal guard:** a multi-file torrent whose entry paths contain `..` or an absolute component is rejected/sanitized — a hostile `.torrent` **cannot write outside** the download dir. *(Proof: a test with a `../../etc/x` path entry.)*
- [ ] **Allocation is bounded everywhere:** every length read off the wire (bencode string, peer message, UDP field) is checked against a cap **before** allocating — a hostile peer/tracker can't OOM you. *(Proof: oversized-field tests across bencode + peer.)*
- [ ] **Resource caps enforced:** total connections, in-flight requests, and per-peer buffers are bounded; a peer flooding `request`s can't exhaust memory/CPU. *(Proof: `docs/19-design.md` names the caps; a flood test stays bounded.)*
- [ ] The tracker `key`/`peer_id` are stable per run and **never logged raw** alongside anything that would deanonymize a peer.

### Observability
- [ ] A `tracing` span per **peer session** and per **announce**, carrying the infohash + peer addr (via `common-telemetry`). *(Proof: spans visible in logs.)*
- [ ] `/metrics` exports **bytes down/up, pieces verified (ok\|failed), peers connected, peers unchoked, announces (http\|udp)** — and the unchoked-peers gauge is what proves the V6 slot cap. *(Proof: a metrics-endpoint test.)*
- [ ] Live **progress + rates** are queryable at `GET /torrents/{info_hash}` while a download runs.

### Ship it
- [ ] **Graceful shutdown**: stop accepting peers, flush in-flight piece writes, announce `stopped` to trackers, then exit — no half-written pieces (ties the V6 shutdown box to the app lifecycle).
- [ ] A `Dockerfile` builds a release image; `/healthz` is a readiness probe.

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load harness lives in `bench/`, the
   numbers in `docs/19-benchmarks.md`.
3. `docs/19-design.md` records the decisions the SPEC grades: **piece-selection
   strategy, the choke/unchoke policy, and the resource caps** (with the trade-offs).
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p bittorrent` are green;
   no `todo!()` remains on a checked path.

## 🐉 Boss fight — The Flash Crowd

> You seed a fresh release. Someone posts the link to a big forum. Within seconds,
> **hundreds of clients** discover your single seed and swarm it — every one of them
> wants every piece, right now, all at once. A naive seeder tries to serve them all,
> its upload bandwidth shatters into hundreds of useless trickles, its buffers balloon,
> and the whole swarm grinds to a halt at 3%. The choke algorithm exists to defeat
> exactly this: serve a few peers *well*, rotate fairly, and let the pieces you hand out
> get re-shared so the swarm heals itself.

**Arena:** `bench/` harness against a **release build** (`cargo run --release`, seeder
on). It spins up a flood of leechers — containers of the reference client, or a load
tool that opens many concurrent peer sessions — all fetching the **same** torrent from
your one seeder, and measures completion, the instantaneous unchoke count, aggregate
throughput, and the seeder's RSS.

**The boss falls when ALL true:**
- [ ] ≥ **50 concurrent leechers** each complete a full download, and **every** output verifies (SHA-1/SHA-256) — **zero corrupt files** under load.
- [ ] The seeder holds **≤ K+1 peers unchoked** at any instant (upload-slot cap + optimistic), proven from the `peers_unchoked` metric — it never fans out to all 50.
- [ ] Sustained aggregate seed throughput ≥ **500 MB/s** over loopback (or the NIC's line rate — hardware noted).
- [ ] Seeder memory stays **bounded** (e.g. RSS < 200 MB) regardless of peer count — no whole-file-per-peer buffering.
- [ ] p99 **time-to-first-block** for a newly-arriving leecher during the storm ≤ **250 ms** — optimistic unchoke keeps newcomers from starving.

**Proof:** methodology + numbers in `docs/19-benchmarks.md` (hardware noted, commands
reproducible via `bench/`).

## Suggested order of attack
1. **Bencode** (V1) — nothing else parses without it. Get the byte-exact round-trip.
2. **Metainfo + infohash** (V2) — parse a real `.torrent`, match its infohash.
3. **Tracker** (V3) — HTTP announce first (easier to eyeball), then the UDP protocol.
4. **Peer wire** (V4) — handshake, then robust message framing off the stream.
5. **Download + verify** (V5) — leech a real small torrent end-to-end; assert the hash.
6. **Seeder + choke** (V6) — serve a leecher, then cap the slots.
7. **Benchmark** the swarm, document the decisions, tune.

## Run the dependencies
```bash
docker compose up -d        # opentracker (HTTP+UDP) + a transmission reference peer
cp .env.example .env        # then adjust if needed
cargo run -p bittorrent     # control plane on :8080; set RUN_SEEDER=true to seed
```
