//! Shared newtypes for the two 20-byte identities that thread through every module.
//!
//! This is plumbing, not a challenge — it's fully implemented (like the `common-*`
//! crates). The *interesting* thing about these types is conceptual, and lives in the
//! SPEC: an [`InfoHash`] is content-addressing (SHA-1 of the bencoded info dict, V2),
//! and a [`PeerId`] is a per-run random identity (see [`crate::client::generate_peer_id`]).

use std::fmt;

/// The 20-byte SHA-1 of a torrent's bencoded `info` dictionary — the name every peer
/// and tracker uses for this content. Computed in [`crate::metainfo`] (V2).
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub struct InfoHash(pub [u8; 20]);

impl InfoHash {
    /// The raw 20 bytes — this is what goes on the wire (tracker query, handshake),
    /// *not* the hex string.
    pub fn as_bytes(&self) -> &[u8; 20] {
        &self.0
    }

    /// Lowercase hex (40 chars) — for URLs, logs, and the control-plane JSON.
    pub fn to_hex(&self) -> String {
        hex::encode(self.0)
    }

    /// Parse a 40-char hex string back into an infohash (`None` if it isn't 20 bytes).
    pub fn from_hex(s: &str) -> Option<Self> {
        let bytes = hex::decode(s).ok()?;
        let arr: [u8; 20] = bytes.try_into().ok()?;
        Some(Self(arr))
    }
}

impl fmt::Display for InfoHash {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_hex())
    }
}

impl fmt::Debug for InfoHash {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "InfoHash({})", self.to_hex())
    }
}

/// Serialize as its hex string so `GET /torrents` renders a readable id.
impl serde::Serialize for InfoHash {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_hex())
    }
}

/// This client's 20-byte identity for a run — a client prefix plus random bytes
/// (see [`crate::client::generate_peer_id`]). Sent in the handshake and the announce.
#[derive(Clone, Copy)]
pub struct PeerId(pub [u8; 20]);

impl PeerId {
    pub fn as_bytes(&self) -> &[u8; 20] {
        &self.0
    }

    pub fn to_hex(&self) -> String {
        hex::encode(self.0)
    }
}

impl fmt::Display for PeerId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_hex())
    }
}

impl fmt::Debug for PeerId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "PeerId({})", self.to_hex())
    }
}
