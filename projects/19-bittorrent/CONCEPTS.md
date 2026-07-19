# Concept Bank — Project 19: BitTorrent Client + Seeder

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted. This is the purest protocol workout on the board — no server, no framework, no one to trust.

---

## 🧠 Card 1 — Bencode & canonical bytes *(V1 · `src/bencode.rs`)*

**The problem.** BitTorrent identifies content by hashing its metadata — which means two independent clients must produce the *same bytes* from the same data, or every identity check on the network fails. JSON can't do this (key order, whitespace, number formats all float free). And your decoder must also hand back the **exact original byte span** of a nested value, because the infohash (Card 2) is a hash of original bytes, not of your parsed structure re-serialized.

**The idea.** Bencode: four types — `i42e`, `4:spam`, `l…e`, `d…e` — with canonical rules baked into the format (dict keys sorted as raw bytes, no leading zeros, no whitespace). Byte-strings are *bytes*, not UTF-8 (piece hashes are raw SHA-1). Decode is total on garbage: leading zeros, negative zero, truncation, overrunning lengths, unsorted keys — all clean `Err`s, never panics.

**In the wild:** every torrent client; the canonical-serialization problem itself recurs in JWS/JWT signing, Git tree objects, content-addressable stores, and blockchain transaction hashing — "hash the bytes, and make the bytes deterministic" is a whole genre.

**You own it when you can explain:**
- [ ] Why content-hashing forces canonical encoding, with the JSON counterexample (same data, different bytes, different "identity").
- [ ] Why the decoder must expose byte spans: hashing `encode(decode(x))` breaks the moment your encoder differs by one byte from the original writer's.
- [ ] Why strings are `Vec<u8>`, not `String` — what data in a real `.torrent` is non-UTF-8.
- [ ] The malformed-input catalogue (`i03e`, `i-0e`, truncation, overrun, duplicate keys, trailing junk) and why each must be an error, not a tolerance.
- [ ] The property-test framing: `encode(decode(x)) == x` byte-exact over a corpus of real torrents.

**Depth probes:**
- Where else does "signature/hash over exact bytes" force the same span-preserving design? (JWS detached payloads, Git objects.)
- Why does bencode sort dict keys as *raw bytes* rather than unicode-aware? What would locale-aware sorting do to interop?

**Trap:** parsing into a nice typed structure and re-encoding to compute the infohash. It works on torrents *your* encoder would have produced and fails on everyone else's — the classic "why doesn't my infohash match" bug.

---

## 🧠 Card 2 — The infohash: identity without a registry *(V2 · `src/metainfo.rs`)*

**The problem.** There is no server to ask "is this the file I think it is?" Strangers on the network must agree they're exchanging the same content — with no naming authority, no accounts, no trust.

**The idea.** Identity *is* the content's description: `infohash = SHA-1(exact bytes of the info dict)`. Two clients agree because they independently hashed the same bytes — agreement by mathematics, not by authority. A magnet link is the compressed form of this idea: just the infohash (+ trackers) — the metainfo itself is fetched *from peers*, verified against the hash it must produce. Parse-time consistency checks (`piece_count`, `total_length` arithmetic) mean a doctored torrent is rejected before it wastes your bandwidth.

**In the wild:** content addressing is Git commits, Docker image digests, IPFS CIDs, Nix store paths — and project 06's blob store; BitTorrent got there first at internet scale.

**You own it when you can explain:**
- [ ] "Identity derived from bytes" vs "identity granted by a registry" — what each requires operationally and what each makes impossible (renaming vs squatting).
- [ ] Why the hash covers the `info` dict specifically (piece hashes, sizes, layout — the *content contract*) and not the outer dict (trackers can change without changing identity).
- [ ] The magnet-link bootstrap: how you can join a swarm knowing only a hash, and how fetched metadata is verified.
- [ ] Byte-sensitivity as a feature: one flipped byte = a different torrent = zero accidental cross-swarm pollution.
- [ ] Single-file vs multi-file layout and what the consistency checks catch.

**Depth probes:**
- SHA-1 is broken for collision resistance. What attack does that enable against torrents, and what did BitTorrent v2 change (SHA-256, per-file merkle trees)?
- Compare with project 06's content-addressed store: same idea, different granularity — what does per-piece hashing add that whole-file hashing lacks (verify-as-you-go, Card 5)?

**Trap:** treating hex and base32 magnet encodings as different identities. Same 20 bytes, two spellings — normalize before comparing or the "same" torrent forks in your UI.

---

## 🧠 Card 3 — Trackers: discovery over HTTP and raw UDP *(V3 · `src/tracker.rs`)*

**The problem.** You have an infohash; you need *addresses* — who else has this content, right now? The tracker is the one semi-centralized rendezvous in the design, so it must be cheap enough to answer swarms of clients, and losing one tracker must not kill the download.

**The idea.** HTTP announce: a GET whose `info_hash`/`peer_id` are raw 20-byte values percent-encoded byte-for-byte (not text!), answered with a bencoded reply carrying a **compact** peer list — 6 bytes per peer (IPv4 + big-endian port), because at swarm scale, structured encodings multiply into real bandwidth. UDP announce (BEP 15): a binary protocol where a `connect` round-trip mints a short-lived connection-id — proof you can receive at your claimed address, killing spoofed-source amplification — then `announce`, all big-endian, paired by transaction id. Announces are periodic side effects (`started`/`stopped`, honor the interval), and tracker failure falls through to the next tracker.

**In the wild:** opentracker runs much of the public torrent world; the UDP connection-id trick is the same anti-spoofing shape as TCP's handshake, SYN cookies, and QUIC's address validation.

**You own it when you can explain:**
- [ ] The percent-encoding trap: why `info_hash` must be encoded from raw bytes, and what mangling it as UTF-8 produces (a valid-looking announce for a nonexistent swarm).
- [ ] Why compact encoding exists — do the arithmetic at 10k peers × N announces/min.
- [ ] The UDP connection-id handshake as spoofing defense: what attack works without it (reflected/amplified announces from forged sources).
- [ ] Announce etiquette as swarm citizenship: intervals honored, `stopped` on exit — what a client that hammers or ghosts does to a tracker.
- [ ] Multi-tracker resilience: one dead tracker is a fallthrough, never a failed download.

**Depth probes:**
- The DHT (BEP 5, Kademlia) removes the tracker entirely. Sketch how "who has infohash X" becomes a distributed lookup — and what the tracker still does better (freshness, low latency).
- Why does UDP fit tracker traffic so well (tiny, idempotent, loss-tolerant queries) where the peer wire needs TCP?

**Trap:** testing announces only against your own tracker. Percent-encoding and compact-format bugs are *interop* bugs — they only surface against a reference implementation.

---

## 🧠 Card 4 — The peer wire: framing, state, and zero trust *(V4 · `src/peer.rs`)*

**The problem.** A raw TCP socket to a stranger. TCP gives you a byte *stream* — your "message" arrives split across three reads, or two messages arrive glued into one. And the stranger may be hostile: a declared message length of 4 GB is an allocation bomb; garbage state transitions can wedge naive implementations. No framework is coming to help.

**The idea.** The fixed 68-byte handshake first (protocol string, infohash, peer id) — wrong infohash, drop immediately. Then length-prefixed messages pulled off the stream with `read_exact` discipline: read 4 bytes of length, *validate against a cap before allocating*, read exactly that many bytes, parse. Above the framing sits the four-flag state machine — am_choking / am_interested / peer_choking / peer_interested — and the peer's piece bitfield; requests flow only when interested-and-unchoked. Unknown message ids are skipped, not fatal (forward compatibility). 16 KiB blocks bound every transfer unit.

**In the wild:** every binary TCP protocol shares this skeleton — Redis RESP (project 22), Postgres wire, Kafka wire; "length-prefix + read_exact + cap-before-alloc" is the universal recipe, and BitTorrent is the best practice arena because peers are actually adversarial.

**You own it when you can explain:**
- [ ] Why a byte stream ≠ messages, with the split-across-reads and two-in-one-read cases, and why the length prefix (not delimiters) is the framing answer here.
- [ ] The cap-before-allocate discipline: the exact line where a hostile length would OOM you, and where the check goes instead.
- [ ] The choke/interest state machine: what each flag means, who sets it, and why `request` before `unchoke` is a protocol violation.
- [ ] Why unknown ids and reserved handshake bits are ignored — how BitTorrent shipped extensions (DHT, fast peers, magnet metadata) without breaking old clients.
- [ ] Keep-alives (zero-length frames) and connection lifecycle on a network where peers vanish silently.

**Depth probes:**
- Why does the handshake put the infohash *before* peer identification? (Swarm routing: one socket, which torrent?)
- Compare this framing with RESP's (project 22): text-prefixed vs binary length — what does each optimize?

**Trap:** `Vec::with_capacity(declared_len)` before validating `declared_len`. One malicious peer, one allocation, OOM. The bug is one line and the discipline is the whole card.

---

## 🧠 Card 5 — Rarest-first, verify-first: assembling from strangers *(V5 · `src/download.rs`)*

**The problem.** Two temptations ruin swarms. Downloading pieces *in order* means everyone has the same early pieces and nobody has the late ones — the swarm converges on scarcity, and when the seed leaves, the tail is gone forever. And *trusting* received bytes means one lying peer corrupts your file invisibly — you'd learn at the end, when the whole file's hash fails, with no idea which peer or which piece.

**The idea.** **Rarest-first**: pick the piece fewest peers have (computed from their bitfields) — you maximize what you can trade *and* keep rare pieces replicated, which is swarm-level altruism emerging from local greed. **Verify-before-write**: every piece is SHA-1-checked against the metainfo before it counts; a bad piece is discarded and refetched — a liar wastes your bandwidth, never your integrity. **Pipelining** keeps several block requests in flight per peer (one-at-a-time turns throughput into a round-trip counter). **Endgame** duplicates the final blocks' requests across peers so one stall doesn't hold the finish line. **Resume** re-verifies disk pieces on restart instead of refetching.

**In the wild:** rarest-first is the celebrated emergent-cooperation result of BitTorrent research; verify-then-trust is the download pattern of apt/nix/docker (fetch from anywhere, verify against a known hash); pipelining is TCP window thinking at the application layer.

**You own it when you can explain:**
- [ ] The sequential-download death spiral (abundance of early pieces, extinction of late ones) and how rarest-first reverses the gradient.
- [ ] Verify-before-write as the trust boundary: what a hostile peer *can* cost you (bandwidth, time) vs *cannot* (corruption) — and why per-piece hashes beat one whole-file hash for blame assignment.
- [ ] The pipelining arithmetic: blocks-in-flight × block size vs bandwidth-delay product — why one outstanding request caps throughput at RTT speed.
- [ ] Endgame's deliberate waste: duplicate requests as insurance against the last slow peer, and why it's only sane at the tail.
- [ ] Resume as verification, not bookkeeping: trust the disk's hashes, not a state file's claims.

**Depth probes:**
- Rarest-first is *global* information estimated from *local* bitfields. How wrong can the estimate be, and does it matter?
- Why 16 KiB blocks inside ~256 KiB–4 MiB pieces — what do the two granularities separately optimize (request scheduling vs hash/announce overhead)?

**Trap:** counting a piece as "have" when its last block arrives rather than when its hash verifies. Announcing an unverified `have` propagates a poisoned piece to the swarm — you become the liar.

---

## 🧠 Card 6 — The choke algorithm: fairness under a flash crowd *(V6 · `src/seeder.rs`)*

**The problem.** You seed; a hundred leechers connect; every one wants everything now. Serve them all "fairly" and your upload bandwidth shatters into a hundred trickles too slow to help anyone — buffers balloon, the swarm crawls at 3%. Finite upload bandwidth *forces* a scheduler; the only question is whether it's deliberate or emergent chaos.

**The idea.** Serve a few peers *well*: at most K **upload slots** (unchoked peers), re-evaluated every ~10 s — plus one **optimistic unchoke** rotating every ~30 s to a random choked peer, so newcomers get a first block and better trading partners get discovered. Everyone else waits, cheaply. The pieces you hand out get re-shared, so swarm capacity *grows* with demand — the flash crowd heals itself, but only because you refused to serve it all at once. Per-peer memory stays bounded (stream blocks from disk); connections are capped; requests for pieces you lack are refused, not panicked over.

**In the wild:** the choke algorithm is BitTorrent's famous tit-for-tat mechanism (leechers unchoke their best *reciprocators*; seeds their best *downloaders*); the deeper pattern — bounded concurrency beats fair-share-of-nothing — is nginx worker pools, DB connection limits, and every admission controller.

**You own it when you can explain:**
- [ ] The bandwidth-fragmentation argument: why K good streams beat 100 useless ones, with the throughput math.
- [ ] The two-tier policy: what regular unchokes select for (throughput) vs what the optimistic slot exists to discover (newcomers, hidden fast peers) — exploration vs exploitation, literally.
- [ ] Tit-for-tat as incentive design: why leeching clients that never upload get choked into the slow lane, and how that makes the protocol robust to freeloaders.
- [ ] Why the swarm scales *up* with demand (each satisfied leecher becomes a partial seed) — the property no client-server design has.
- [ ] The bounded-serving hygiene: streamed blocks, capped connections, refused invalid requests — a swarm of strangers as your traffic model.

**Depth probes:**
- Time-to-first-block for a newcomer during a storm is bounded by what, exactly? (The optimistic rotation period and your slot count.)
- What K is right for a given upload bandwidth, and what does too-large K do that too-small K doesn't?

**Trap:** unchoking generously "since it's just fairness tuning". The slot cap is a *correctness-under-load* mechanism — without it the flash crowd is a self-inflicted DDoS, which is why the boss fight measures unchoke counts, not politeness.

---

## ⚡ Rapid-fire round

- [ ] Path traversal via hostile multi-file torrents: a `../../etc/x` path entry must never escape the download dir.
- [ ] Every wire-read length checked before allocation — bencode strings, peer messages, UDP fields alike.
- [ ] Resource caps as a set: connections, in-flight requests, per-peer buffers — a `request`-flooding peer exhausts itself, not you.
- [ ] `peer_id` conventions (client prefix + random, stable per run) and why it's never logged raw next to identifying data.
- [ ] Graceful shutdown: flush in-flight piece writes, announce `stopped` — a polite exit from the swarm.

## 🔗 Connects to

- Content addressing is project 06's blob store and Card 2's infohash — same identity idea at file, object, and swarm scale.
- Length-prefixed framing off a raw socket returns in project 22's RESP codec — this project is where the discipline is learned adversarially.
- The choke algorithm's bounded-slots idea is admission control — the same shape as project 10's load shedding and connection caps.
