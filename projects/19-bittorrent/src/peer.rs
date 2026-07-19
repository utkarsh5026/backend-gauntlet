//! V4 — The peer wire protocol: the raw-TCP conversation between two peers.
//!
//! Once you have a peer's address (V3), you open a TCP connection and speak the wire
//! protocol directly — no HTTP to hide behind. It's two phases:
//!
//!   1. **Handshake** — a fixed 68 bytes: `<19>"BitTorrent protocol"<8 reserved bytes>
//!      <20-byte infohash><20-byte peer_id>`. If the peer's infohash isn't the one you
//!      asked for, you hang up before exchanging anything.
//!   2. **Messages** — a stream of `<4-byte big-endian length><1-byte id><payload>`
//!      frames. A length of `0` is a keep-alive (no id). The core ids: `choke(0)`,
//!      `unchoke(1)`, `interested(2)`, `not_interested(3)`, `have(4)`, `bitfield(5)`,
//!      `request(6)`, `piece(7)`, `cancel(8)`.
//!
//! Two things make this a real exercise. First, **framing**: TCP is a byte stream, so a
//! message can arrive split across reads, or two can arrive in one read — you must
//! reassemble by the length prefix (`read_exact` the length, then the body). Second,
//! the **choke/interest state machine**: each side tracks four booleans, and you only
//! send data to a peer you've unchoked who is interested. And never trust a peer: cap
//! every declared length *before* you allocate, or a hostile peer OOMs you with one
//! 4 GiB "message".

use crate::error::AppError;
use crate::types::{InfoHash, PeerId};

/// The fixed protocol string in the handshake (`pstr`), length 19.
pub const PROTOCOL: &[u8; 19] = b"BitTorrent protocol";

/// Blocks are requested in ≤ 16 KiB chunks; a peer requesting more is refused.
pub const BLOCK_SIZE: u32 = 16 * 1024;

/// Reject any message whose declared length exceeds this — the anti-OOM cap. A real
/// `piece` message is a block (~16 KiB) plus a small header, so 1 MiB is generous.
pub const MAX_MESSAGE_LEN: u32 = 1 << 20;

/// The 68-byte handshake, parsed.
#[derive(Debug, Clone)]
pub struct Handshake {
    pub info_hash: InfoHash,
    pub peer_id: PeerId,
}

impl Handshake {
    /// Serialize to the 68 bytes on the wire.
    ///
    /// TODO(V4): `[19][b"BitTorrent protocol"][8 zero reserved bytes][info_hash][peer_id]`.
    pub fn encode(&self) -> [u8; 68] {
        todo!("V4: build the 68-byte handshake")
    }

    /// Parse and validate 68 received bytes.
    ///
    /// TODO(V4): check the first byte is 19 and the next 19 are the protocol string;
    /// extract the infohash and peer-id. A caller compares the infohash to the one it
    /// dialed for and drops the peer on mismatch (that check is the security boundary).
    pub fn decode(buf: &[u8; 68]) -> Result<Self, AppError> {
        let _ = buf;
        todo!("V4: validate pstrlen/pstr, then extract infohash + peer_id")
    }
}

/// A peer wire message. `KeepAlive` is the zero-length frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Message {
    KeepAlive,
    Choke,
    Unchoke,
    Interested,
    NotInterested,
    Have(u32),
    /// One bit per piece: bit `i` set = the peer has piece `i` (high bit first).
    Bitfield(Vec<u8>),
    Request {
        index: u32,
        begin: u32,
        length: u32,
    },
    Piece {
        index: u32,
        begin: u32,
        block: Vec<u8>,
    },
    Cancel {
        index: u32,
        begin: u32,
        length: u32,
    },
}

impl Message {
    /// Serialize to a framed `<len><id><payload>` (or the 4 zero bytes of a keep-alive).
    ///
    /// TODO(V4): write the 4-byte big-endian length, then the id + payload for each
    /// variant. `KeepAlive` is just `[0,0,0,0]`.
    pub fn encode(&self) -> Vec<u8> {
        todo!("V4: frame this message (4-byte BE length + id + payload)")
    }

    /// Build a message from an already-deframed `id` + `body` (length prefix consumed).
    ///
    /// TODO(V4): map the id to its variant and slice the fixed-width fields out of
    /// `body`. Reject an id/length that doesn't match (e.g. a `have` whose body isn't
    /// 4 bytes) as [`AppError::Peer`] — don't index past the slice.
    pub fn decode(id: u8, body: &[u8]) -> Result<Message, AppError> {
        let _ = (id, body);
        todo!("V4: id + body → Message variant, with bounds checks")
    }
}

/// The four-flag state each side tracks about a connection, plus the peer's bitfield.
/// Both peers start out choking and uninterested — nothing flows until that changes.
#[derive(Debug, Clone)]
pub struct PeerState {
    pub am_choking: bool,
    pub am_interested: bool,
    pub peer_choking: bool,
    pub peer_interested: bool,
    /// Which pieces the peer has advertised (via `bitfield` + `have`).
    pub peer_bitfield: Vec<bool>,
}

impl Default for PeerState {
    fn default() -> Self {
        Self {
            am_choking: true,
            am_interested: false,
            peer_choking: true,
            peer_interested: false,
            peer_bitfield: Vec::new(),
        }
    }
}

impl PeerState {
    /// Fold an incoming message into the connection state (flags + bitfield).
    ///
    /// TODO(V4): update the flags on `Choke`/`Unchoke`/`Interested`/`NotInterested`,
    /// set a bit on `Have`, and replace the bitfield on `Bitfield`. This is the state
    /// the download loop (V5) reads to decide what to request and the seeder (V6) reads
    /// to decide whom to unchoke.
    pub fn apply(&mut self, msg: &Message) {
        let _ = msg;
        todo!("V4: advance the choke/interest state machine from a received message")
    }
}

/// Read exactly one message off an async byte stream, reassembling across TCP reads.
///
/// TODO(V4): `read_exact` the 4-byte length; `0` → `KeepAlive`. Otherwise reject a
/// length > [`MAX_MESSAGE_LEN`] *before* allocating, then `read_exact` that many bytes,
/// split off the id, and hand the rest to [`Message::decode`]. This function is where
/// "TCP is a stream, not a message queue" actually bites.
pub async fn read_message<R>(reader: &mut R) -> Result<Message, AppError>
where
    R: tokio::io::AsyncRead + Unpin,
{
    let _ = reader;
    todo!("V4: frame one message off the stream (length-prefix, cap, read_exact)")
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove the protocol.
    //   - handshake encode→decode round-trips; a wrong pstr / wrong infohash is rejected;
    //   - every Message variant encode→decode round-trips;
    //   - FRAMING: feed a two-message byte buffer one byte at a time to `read_message`
    //     and assert both messages come out intact (split reads reassemble);
    //   - an oversized declared length (> MAX_MESSAGE_LEN) is rejected without a huge
    //     allocation;
    //   - `PeerState::apply` flips the right flag for each control message.
    //   - bonus (needs `docker compose up`): complete a real handshake + bitfield
    //     exchange against the `transmission` reference peer.
}
