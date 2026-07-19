# ICE & STUN — Reachability Before Transport, From First Principles

> How a browser behind NAT finds and *proves* a path to your server before one
> media byte flows. No prior networking knowledge assumed beyond "UDP sends
> datagrams to an IP:port".
>
> Prepares you for **V1** in [SPEC.md](../SPEC.md) — the STUN codec + ICE-lite
> agent in [ice.rs](../src/ice.rs) (`StunMessage::parse` / `encode`,
> `message_integrity`, `fingerprint`, `IceAgent::handle` — all currently
> `todo!()`). The muxed socket that feeds it is wired in
> [pump.rs](../src/pump.rs) and [wire.rs](../src/wire.rs).

---

## 0. The one sentence to hold onto

**Before transport comes reachability: ICE is how two endpoints discover a
working address pair through NAT, and STUN's authentication is what stops an
open UDP port from letting any stranger nominate themselves into your call.**

---

## 1. The problem: NAT breaks "just send a packet to the peer"

Your laptop's address is something like `192.168.1.23`. That address is
**private** — millions of machines have the same one; it means nothing outside
your home network. Your router (the NAT — Network Address Translator) owns the
one public address, and it maintains a mapping table:

```
  laptop 192.168.1.23:51000  ──▶  NAT  ──▶  internet sees 203.0.113.7:62031
                                   │
                                   └── mapping created ONLY when the laptop
                                       sends outbound; inbound packets that
                                       match no mapping are DROPPED
```

Now try to build a call between two browsers naively:

| Naive plan | Why it fails |
|---|---|
| "Send media to the peer's IP." | The peer only knows its *private* address (`192.168.1.23`). Sending there routes nowhere. |
| "Okay, learn your public address and advertise that." | The machine can't see its own public address — the NAT invents it. Something *outside* must reflect it back. |
| "Fine, someone told me the public address; send to it." | The NAT **drops unsolicited inbound**. Until the peer has sent *outbound* from that mapping, nothing gets in. |
| "Both sides just start sending, then." | Which of a machine's several plausible addresses (LAN, public-reflexive, VPN…) actually works depends on both NATs. You need to *test* pairs, not guess. |

So connectivity is a *search problem*: each side gathers **candidate**
addresses, and the pairs are tested until one demonstrably works. That search
is **ICE** (Interactive Connectivity Establishment), and the test packets are
**STUN** messages.

### Why this project's side is easy mode: ICE-lite

The full search is symmetric and hairy. But this SFU is a *server on a public
address* — it has no NAT problem of its own. RFC 8445 carves out **ICE-lite**
for exactly this case: the server never gathers candidates or sends its own
checks; it just **answers the browser's checks correctly** and remembers which
source address won. The browser does the driving. Answering *correctly* is
still the whole vertical, because — the trap in
[CONCEPTS.md](../CONCEPTS.md) — a browser **silently discards** any response
with one wrong byte, and the failure looks like "call never connects" with no
error anywhere.

## 2. STUN: the message format

A STUN message is a fixed 20-byte header followed by attributes
(see the diagram in [ice.rs](../src/ice.rs)):

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|0 0|     STUN Message Type      |         Message Length        |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                   Magic Cookie = 0x2112A442                   |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                  Transaction ID (96 bits)                     |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|          Attributes: TLVs, each padded to 4 bytes …           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

Piece by piece:

- **Top two bits `00`** — this is also what makes STUN demuxable from RTP on a
  shared port: a STUN first byte is `0x00..=0x03`, an RTP first byte is
  `0x80..=0xBF` (see [`classify`](../src/wire.rs) and doc
  [05](05-the-wire-and-the-guardrails.md)).
- **14-bit message type** — a *class* (request / indication / success /
  error, the [`StunClass`](../src/ice.rs) enum) and a 12-bit *method*
  (Binding, `0x001`, is the only one ICE uses here) packed together, with the
  two class bits *interleaved into* the method bits rather than adjacent.
  The two values you'll see constantly: **Binding request = `0x0001`**,
  **Binding success response = `0x0101`**.
- **Length** — of the attributes only (header excluded), and it plays a
  starring role in the integrity computation below.
- **Magic cookie `0x2112A442`** — a fixed constant in every RFC 5389 message.
  Two jobs: it lets a receiver cheaply reject non-STUN bytes, and it's the XOR
  mask for address obfuscation (§4).
- **Transaction ID** — 96 random bits chosen by the requester. The response
  **echoes it verbatim**; that's how the browser matches a response to the
  check it sent. Echo the wrong txid → silent discard.

**Attributes** are TLVs: 2-byte type, 2-byte length, then the value, then
zero-padding to the next 4-byte boundary. The ones this SFU models are the
[`StunAttribute`](../src/ice.rs) enum: `USERNAME`, `XOR-MAPPED-ADDRESS`,
`MESSAGE-INTEGRITY`, `FINGERPRINT`, `PRIORITY`, `USE-CANDIDATE`,
`ICE-CONTROLLING`/`ICE-CONTROLLED`.

> **The parser's oath.** This port is open UDP: *anyone on the internet* can
> send it arbitrary bytes. Every length you read is an attacker-controlled
> number until you've range-checked it against the buffer. The V1 criteria
> demand parsing be *total on garbage* — a runt datagram, a wrong cookie, or
> an attribute length that overruns the buffer is a clean `Err`
> ([`SfuError::Truncated` / `BadMagic` / `Malformed`](../src/error.rs)), never
> a panic or out-of-bounds read. `parse` in the scaffold already shows the
> first two checks; the TLV walk is yours.

## 3. The ICE dance, from the SFU's chair

Signaling happens first, over HTTP ([signaling.rs](../src/signaling.rs)): the
browser and SFU exchange short credentials — a `ufrag` (username fragment) and
a `pwd` (password) each — plus the SFU's media address. Then, on the media
port:

```
 browser (behind NAT)                        SFU (public, ICE-lite)
        │                                          │
        │  STUN Binding request                    │
        │  USERNAME: "<sfu-ufrag>:<browser-ufrag>" │
        │  PRIORITY, ICE-CONTROLLING               │
        │  MESSAGE-INTEGRITY (keyed by SFU's pwd)  │
        │  FINGERPRINT                             │
        │─────────────────────────────────────────▶│  verify USERNAME + integrity
        │                                          │  (wrong? drop. NEVER nominate.)
        │  STUN Binding success response           │
        │  (echoes txid)                           │
        │  XOR-MAPPED-ADDRESS = source addr seen   │
        │  MESSAGE-INTEGRITY + FINGERPRINT         │
        │◀─────────────────────────────────────────│
        │                                          │
        │  … more checks; then the browser decides │
        │  and re-sends with USE-CANDIDATE ───────▶│  NOMINATED: this source addr
        │                                          │  is now the peer's media path
        │  RTP flows (accepted from that addr only)│
        │◀────────────────────────────────────────▶│
```

Three things to internalize from that picture:

1. **The check itself creates the NAT mapping.** The browser's outbound STUN
   punches the hole that the SFU's response (and later media) rides back
   through. That's why reachability must be *demonstrated*, not assumed.
2. **The response's `XOR-MAPPED-ADDRESS` tells the browser what the world
   sees** — its server-reflexive address. That is the "something outside must
   reflect it back" from §1.
3. **Nomination is just a flag on a check.** `USE-CANDIDATE` on a valid,
   authenticated request means: this pair is *the one*. The SFU records the
   packet's source address, and from then on RTP/RTCP from that address routes
   to that peer — and from nowhere else (doc
   [05](05-the-wire-and-the-guardrails.md)).

In [ice.rs](../src/ice.rs) this is [`IceAgent::handle`](../src/ice.rs)
returning an [`IceAction`](../src/ice.rs): `Respond(bytes)`, and additionally
`Nominated { peer }` when `USE-CANDIDATE` was present and authentic.

## 4. XOR-MAPPED-ADDRESS: a worked example

Why is the reflected address *XORed* instead of sent plainly? Because of NAT
**ALGs** (Application Layer Gateways) — middleboxes that grep packet payloads
for anything shaped like their own IP address and "helpfully" rewrite it. An
address XORed with the magic cookie no longer looks like an address, so the
meddling misses it; the receiver un-XORs and gets the truth.

The rule (IPv4): X-Port = port ⊕ (top 16 bits of the cookie); X-Address =
address ⊕ cookie. Say the SFU saw the check arrive from `192.0.2.1:3478`:

| Field | Plain | XOR mask | On the wire |
|---|---|---|---|
| Port | `3478` = `0x0D96` | `0x2112` | `0x2C84` (= 11396) |
| Address | `192.0.2.1` = `0xC0000201` | `0x2112A442` | `0xE112A643` (reads as 225.18.166.67) |

For **IPv6**, the 128-bit address is XORed with the cookie *concatenated with
the 96-bit transaction id* — which is why the V1 round-trip criterion calls
out both families: the IPv6 path forces your codec to thread the txid into
attribute decoding.

## 5. Authentication: MESSAGE-INTEGRITY and FINGERPRINT

An open UDP port receives datagrams from *anyone*. Without authentication, a
stranger who learns your media address could fire a Binding request with
`USE-CANDIDATE` and **nominate their own address as the peer's media path** —
hijacking the call's media. That's the attack `MESSAGE-INTEGRITY` closes, and
why the V1 criterion says an unauthenticated check "is dropped and never
nominates a path".

- **MESSAGE-INTEGRITY** is an HMAC-SHA1 (20 bytes) over the message, keyed by
  the ICE `pwd` that was exchanged over signaling. Only someone who was
  *given* the pwd can produce a valid check. HMAC-SHA1 always yields 20
  bytes — e.g. keyed with `pass1234pass1234pass1234` over a sample 20-byte
  header it comes out `2138aba31f96186f…` (20 bytes, verified) — and
  [`message_integrity`](../src/ice.rs) is the one place the `hmac`/`sha1`
  crates appear.
- **FINGERPRINT** is `CRC32(message) ^ 0x5354554E`. Not security — CRC32 has
  no key — just a cheap "this really is STUN" checksum. The XOR constant is
  the ASCII bytes `"STUN"` (`0x53 0x54 0x55 0x4E`), a deliberate signature.

**The bookkeeping trap** (named in the scaffold's `encode` TODO, and the
part most worth slowing down for): both attributes are computed over the
message *so far*, then appended — integrity first, fingerprint last — and the
header's **length field must be set as if the attribute being computed were
already present**. Get the order or the length wrong and your HMAC is valid
over the wrong bytes: the browser recomputes, mismatches, and silently drops.
How you structure encode-then-sign-then-append is yours to design — it's the
interesting part of `encode`.

## 6. The design space (what's yours to decide)

The SPEC fixes *what*; these are the *hows* you'll choose in
[ice.rs](../src/ice.rs) and record in `docs/15-design.md`:

- **The TLV walk.** How you iterate attributes so that every slice is
  bounds-checked before indexing, padding is skipped correctly, and unknown
  attributes are carried through (the `Unknown` variant) without breaking
  round-trip identity.
- **Encode/sign ordering.** How `encode` stages the buffer so the
  length-as-if-appended rule holds for MESSAGE-INTEGRITY and then FINGERPRINT.
- **The verification discipline in `handle`.** What you check, in what order
  (USERNAME shape, integrity, only then any side effect), and the rule that
  *nothing* mutates `nominated` before authentication passes.
- **Scope honesty.** ICE-lite means no TURN, no mDNS candidates, no ICE
  restart — the design doc states what's out and why that's fine for a public
  server.

When you're ready to build: `/quest` scaffolds the acceptance tests first;
`/hint` if you get stuck mid-way.

## 7. Mental model summary

| Piece | Job | One-line why |
|---|---|---|
| NAT | Maps private↔public, drops unsolicited inbound | The reason reachability is a problem at all |
| ICE | Test candidate address pairs, nominate a winner | Connectivity is discovered, not assumed |
| ICE-lite | Answer checks only, never send them | A public server has nothing to discover about itself |
| STUN Binding request/response | The test packet + its receipt | The check *is* what punches and proves the path |
| Transaction ID | Matches response to request | Wrong echo = silent discard |
| Magic cookie | Marks STUN + masks addresses | Cheap reject + ALG defeat |
| XOR-MAPPED-ADDRESS | "Here's the address I saw you as" | The outside reflection NAT'd hosts need |
| MESSAGE-INTEGRITY | HMAC-SHA1 keyed by the signaled `pwd` | The port's auth layer — no pwd, no nomination |
| FINGERPRINT | CRC32 ^ `"STUN"` | "Really STUN", not security |
| USE-CANDIDATE | Nominate this pair | From here on, media is accepted from this address |

## 8. Where you'll build this

Everything lands in [ice.rs](../src/ice.rs): `StunMessage::parse`,
`StunMessage::encode`, `message_integrity`, `fingerprint`, and
`IceAgent::handle` — each `todo!()` is annotated with its contract. The wired
[pump](../src/pump.rs) already routes every STUN-classified datagram to
[`Sfu::handle_stun`](../src/sfu.rs), so the first real browser check will hit
your `parse` immediately.

This doc unlocks V1's **Done when ALL true** (see [SPEC.md](../SPEC.md)):
codec round-trips (IPv4 + IPv6), total-on-garbage parsing, integrity
verifies/rejects, valid success responses, and USE-CANDIDATE nomination —
proven by `stun_binding_roundtrips`, `short_stun_errors`, `bad_cookie_errors`,
`message_integrity_verifies`, `use_candidate_nominates`.
