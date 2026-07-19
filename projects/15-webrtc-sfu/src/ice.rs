//! V1 — ICE / STUN connectivity: let a browser behind NAT actually reach the SFU.
//!
//! Before a single media byte flows, the two sides have to find a path through NAT and
//! prove to each other that the path works. That is **ICE**, and its packets are **STUN**
//! messages. Signaling (the HTTP plane) hands each side the other's `ufrag`/`pwd` and a
//! list of candidate transport addresses; then each side fires **STUN Binding requests** at
//! the other's candidates and watches for **success responses**. The first candidate pair
//! that completes a check round-trip and gets **nominated** becomes the path media rides.
//! This SFU is an **ICE-lite** server: it doesn't gather reflexive candidates or send its
//! own checks, it just answers the browser's checks correctly and remembers which source
//! address won — but answering *correctly* is the whole vertical.
//!
//! A **STUN message** is a 20-byte header + typed attributes:
//!
//! ```text
//!  0                   1                   2                   3
//!  0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! |0 0|     STUN Message Type      |         Message Length        |
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! |                         Magic Cookie (0x2112A442)             |
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! |                     Transaction ID (96 bits)                  |
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! |            Attributes: TLV, each 4-byte aligned …             |
//! +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//! ```
//!
//! The Binding **response** must echo the transaction id, carry an **XOR-MAPPED-ADDRESS**
//! (the source address the SFU saw, XORed with the magic cookie + txid so NATs don't rewrite
//! it), a **MESSAGE-INTEGRITY** (HMAC-SHA1 over the message keyed by the ICE `pwd`), and a
//! **FINGERPRINT** (CRC32 of the message ^ `0x5354554e`). Get any of those wrong and the
//! browser silently discards the response and the call never connects.

use std::net::SocketAddr;

use crate::error::{Result, SfuError};

/// The STUN magic cookie — bytes 4..8 of every RFC 5389 message; also XOR'd into addresses
/// and the transaction-id space.
pub const STUN_MAGIC_COOKIE: u32 = 0x2112_A442;
/// Fixed STUN header size (type + length + cookie + 96-bit transaction id).
pub const STUN_HEADER_LEN: usize = 20;
/// FINGERPRINT XOR constant (RFC 5389 §15.5).
pub const FINGERPRINT_XOR: u32 = 0x5354_554e;

/// The four STUN message classes (top of the 14-bit message type).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StunClass {
    Request,
    Indication,
    SuccessResponse,
    ErrorResponse,
}

/// The 96-bit STUN transaction id (matches a response to its request).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransactionId(pub [u8; 12]);

/// The STUN attributes this SFU cares about (others are parsed as `Unknown` and ignored on
/// read — but a well-behaved comprehension-required unknown must still be handled per spec).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StunAttribute {
    /// `USERNAME` = `<remote-ufrag>:<local-ufrag>` on an inbound check.
    Username(String),
    /// `XOR-MAPPED-ADDRESS` — the reflexive address, obfuscated by cookie ^ txid.
    XorMappedAddress(SocketAddr),
    /// `MESSAGE-INTEGRITY` — 20-byte HMAC-SHA1 over the message (keyed by ICE `pwd`).
    MessageIntegrity([u8; 20]),
    /// `FINGERPRINT` — CRC32(msg) ^ 0x5354554e.
    Fingerprint(u32),
    /// `PRIORITY` of the candidate the peer is checking from.
    Priority(u32),
    /// `USE-CANDIDATE` — the controlling side asks to nominate this pair.
    UseCandidate,
    /// `ICE-CONTROLLING` / `ICE-CONTROLLED` tie-breaker value.
    IceControlling(u64),
    IceControlled(u64),
    /// Any attribute type this SFU doesn't model.
    Unknown {
        attr_type: u16,
        value: Vec<u8>,
    },
}

/// A parsed STUN message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StunMessage {
    pub class: StunClass,
    /// 12-bit method — `0x001` (Binding) is the only one ICE uses on the media port.
    pub method: u16,
    pub transaction_id: TransactionId,
    pub attributes: Vec<StunAttribute>,
}

impl StunMessage {
    /// Parse a STUN message off a datagram.
    ///
    /// TODO(V1): validate the 2 top bits are zero and the magic cookie is `0x2112A442`
    /// (else `Err(BadMagic)`); decode the class from the message-type bits; read the 12-byte
    /// transaction id; then walk the attribute TLVs — each is a 2-byte type, 2-byte length,
    /// value, padded to a 4-byte boundary. **Range-check every length** against the buffer
    /// before slicing (a length that overruns is `Err(Malformed)`, never an OOB read), and
    /// un-XOR `XOR-MAPPED-ADDRESS` with the cookie + txid.
    pub fn parse(buf: &[u8]) -> Result<StunMessage> {
        if buf.len() < STUN_HEADER_LEN {
            return Err(SfuError::Truncated {
                need: STUN_HEADER_LEN,
                got: buf.len(),
            });
        }
        let cookie = u32::from_be_bytes([buf[4], buf[5], buf[6], buf[7]]);
        if cookie != STUN_MAGIC_COOKIE {
            return Err(SfuError::BadMagic(format!("stun cookie {cookie:#010x}")));
        }
        let _ = buf;
        todo!("V1: decode class/method + txid, then walk the attribute TLVs (bounds-checked)")
    }

    /// Encode this message to bytes, appending MESSAGE-INTEGRITY (if `integrity_key` is set)
    /// and FINGERPRINT last, in that order.
    ///
    /// TODO(V1): write the 20-byte header (type from class+method, a *provisional* length,
    /// cookie, txid) then each attribute TLV 4-byte-aligned. MESSAGE-INTEGRITY is special:
    /// its HMAC-SHA1 is computed over the message **with the length field set as if the
    /// integrity attribute were already appended**, keyed by `pwd` — see [`message_integrity`].
    /// FINGERPRINT is computed last over everything before it. The receiver of your response
    /// re-derives both and drops the message on any mismatch, so order + length bookkeeping
    /// is the whole game.
    pub fn encode(&self, integrity_key: Option<&[u8]>) -> Vec<u8> {
        let _ = (integrity_key, self.method, self.class, &self.attributes);
        todo!("V1: serialize header + attributes, then MESSAGE-INTEGRITY then FINGERPRINT")
    }
}

/// Compute the STUN MESSAGE-INTEGRITY MAC: HMAC-SHA1 of `message` keyed by `key` (the ICE
/// `pwd`). `message` must already have its length field set to include the 24-byte integrity
/// attribute that will follow.
///
/// TODO(V1): `Hmac::<Sha1>::new_from_slice(key)`, update with `message`, finalize to 20 bytes.
/// (This is the one place the `hmac`/`sha1` crates are used — the check that proves a check
/// really came from the peer holding the shared `pwd`.)
pub fn message_integrity(message: &[u8], key: &[u8]) -> [u8; 20] {
    let _ = (message, key);
    todo!("V1: HMAC-SHA1(message, key) -> [u8; 20]")
}

/// Compute the STUN FINGERPRINT: `crc32(message) ^ 0x5354554e`.
///
/// TODO(V1): CRC32 (`crc32fast`) over everything up to the fingerprint attribute, XOR the
/// constant. Cheap integrity that lets a receiver reject a non-STUN packet that merely
/// looked like one.
pub fn fingerprint(message: &[u8]) -> u32 {
    let _ = message;
    todo!("V1: crc32(message) ^ FINGERPRINT_XOR")
}

/// What the SFU should do after handling one inbound STUN message.
#[derive(Debug)]
pub enum IceAction {
    /// Answer the check with this encoded Binding success response (send it back to `from`).
    Respond(Vec<u8>),
    /// The pair was nominated — this source address is now the peer's media path.
    Nominated { peer: SocketAddr },
    /// Nothing to send (e.g. a response to our own check, or a stray message).
    Nothing,
}

/// Per-peer ICE state: our local `ufrag`/`pwd` (from signaling) and, once a check succeeds,
/// the remote address that won. An **ICE-lite** agent — it validates and answers the
/// browser's connectivity checks and nominates a pair; it never gathers or sends its own.
pub struct IceAgent {
    local_ufrag: String,
    local_pwd: String,
    remote_ufrag: String,
    nominated: Option<SocketAddr>,
}

impl IceAgent {
    pub fn new(local_ufrag: String, local_pwd: String, remote_ufrag: String) -> Self {
        Self {
            local_ufrag,
            local_pwd,
            remote_ufrag,
            nominated: None,
        }
    }

    /// The source address that won ICE nomination (media flows here), if any yet.
    pub fn peer(&self) -> Option<SocketAddr> {
        self.nominated
    }

    /// Handle one inbound STUN message from `from`.
    ///
    /// TODO(V1): for a **Binding request**, verify the `USERNAME` is `<local-ufrag>:<remote-
    /// ufrag>` and the `MESSAGE-INTEGRITY` checks out against `local_pwd` (drop with
    /// `Err(Integrity)` if not — an unauthenticated check must never nominate a path); then
    /// build a Binding **success response** echoing the txid with `XOR-MAPPED-ADDRESS = from`,
    /// signed with `local_pwd` + fingerprinted, and return [`IceAction::Respond`]. If the
    /// request carried `USE-CANDIDATE`, also record `from` as [`nominated`](Self::peer) and
    /// signal [`IceAction::Nominated`]. A **response** to one of our (non-existent, ICE-lite)
    /// checks is [`IceAction::Nothing`].
    pub fn handle(&mut self, msg: &StunMessage, from: SocketAddr) -> Result<IceAction> {
        let _ = (
            msg,
            from,
            &self.local_ufrag,
            &self.local_pwd,
            &self.remote_ufrag,
            &mut self.nominated,
        );
        todo!("V1: authenticate the Binding request, build the success response, nominate on USE-CANDIDATE")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the ICE plane:
    //   - `stun_binding_roundtrips`: encode∘parse is identity on class/method/txid/attributes
    //     for a Binding request and a Binding success response (incl. XOR-MAPPED-ADDRESS for
    //     both IPv4 and IPv6);
    //   - `short_stun_errors` / `bad_cookie_errors`: a runt or wrong-cookie datagram is `Err`,
    //     never a panic (feed it random bytes with `proptest` — an open UDP port gets those);
    //   - `message_integrity_verifies`: a message signed with `pwd` verifies with `pwd` and
    //     fails with the wrong key; `fingerprint` matches a known vector;
    //   - `use_candidate_nominates`: a Binding request with USE-CANDIDATE + valid integrity
    //     yields `Respond` *and* records the `from` addr as the nominated peer.
}
