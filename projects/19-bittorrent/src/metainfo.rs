//! V2 — Metainfo & the infohash: identity without a registry.
//!
//! Parse a `.torrent` (bencoded, V1) into typed fields, and compute the **infohash =
//! SHA-1(exact bytes of the `info` dictionary)**. That 20-byte hash *is* the torrent's
//! identity — there is no central registry; two clients agree they're talking about the
//! same content because they independently hashed the same info bytes. Get the bytes
//! wrong (re-encode non-canonically) and your infohash won't match anyone's.
//!
//! Also parse `magnet:?xt=urn:btih:<hash>&tr=<tracker>&dn=<name>` links, which carry the
//! infohash and trackers but *not* the metainfo — for a magnet you learn piece length
//! and hashes later, from peers (BEP 9), so a fresh magnet has no `piece_hashes` yet.

use std::path::PathBuf;

use crate::error::AppError;
use crate::types::InfoHash;

/// One file inside a (possibly multi-file) torrent. `path` is relative to the torrent's
/// name/root; it must be sanitized before use (see the security checklist — a hostile
/// `.torrent` can put `..` here to escape the download directory).
#[derive(Debug, Clone)]
pub struct FileEntry {
    pub path: PathBuf,
    pub length: u64,
}

/// A parsed `.torrent`.
#[derive(Debug, Clone)]
pub struct Metainfo {
    pub name: String,
    /// Primary tracker (`announce`), if present.
    pub announce: Option<String>,
    /// Additional trackers (`announce-list`), flattened.
    pub announce_list: Vec<String>,
    /// Bytes per piece (the last piece may be shorter).
    pub piece_length: u64,
    /// One 20-byte SHA-1 per piece, in order — the `pieces` string split into 20s.
    pub piece_hashes: Vec<[u8; 20]>,
    /// Files (one entry for a single-file torrent).
    pub files: Vec<FileEntry>,
    /// Sum of every file length.
    pub total_length: u64,
    /// SHA-1 of the exact `info` bytes.
    pub info_hash: InfoHash,
}

impl Metainfo {
    /// Parse a `.torrent`'s raw bytes.
    ///
    /// TODO(V2): bdecode `torrent` (V1), pull `announce`/`announce-list`, `info.name`,
    /// `info.piece length`, split `info.pieces` into 20-byte hashes, and read the file
    /// list (single-file: `info.length`; multi-file: `info.files[].{length,path}`).
    /// Then compute `info_hash = SHA-1(` the *original* bytes of the `info` value `)` —
    /// use the byte span from [`crate::bencode::decode_dict_with_spans`], not a re-encode.
    /// Validate consistency (see [`Self::check_consistency`]) before returning.
    pub fn from_bytes(torrent: &[u8]) -> Result<Self, AppError> {
        let _ = torrent;
        todo!("V2: bdecode the torrent, extract fields, SHA-1 the exact info bytes")
    }

    /// Number of pieces — must equal `ceil(total_length / piece_length)`.
    pub fn piece_count(&self) -> usize {
        self.piece_hashes.len()
    }

    /// TODO(V2): sanity-check the parse: `piece_count() == ceil(total/piece_length)`,
    /// every piece hash is exactly 20 bytes, `total_length == Σ file lengths`. A torrent
    /// that fails this is [`AppError::InvalidTorrent`], not a panic.
    pub fn check_consistency(&self) -> Result<(), AppError> {
        todo!("V2: verify piece count vs total length and hash-string length")
    }
}

/// A parsed `magnet:` link. No metainfo — just enough to start finding peers.
#[derive(Debug, Clone)]
pub struct MagnetLink {
    pub info_hash: InfoHash,
    pub trackers: Vec<String>,
    pub name: Option<String>,
}

impl MagnetLink {
    /// Parse `magnet:?xt=urn:btih:<hash>&tr=<url>&dn=<name>`.
    ///
    /// TODO(V2): split the query string, require `xt=urn:btih:` and decode the hash
    /// (40-char hex *or* 32-char base32 — real magnets use both), collect every `tr=`
    /// tracker (percent-decoded), and take the optional `dn=` display name.
    pub fn parse(uri: &str) -> Result<Self, AppError> {
        let _ = uri;
        todo!("V2: parse a magnet URI (xt=urn:btih hex|base32, tr=…, dn=…)")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove parsing + identity.
    //   - a checked-in real `.torrent` parses, and its `info_hash.to_hex()` equals the
    //     value a real client/tracker reports for the same file (identity is correct);
    //   - flipping one byte of the info dict yields a *different* infohash;
    //   - a multi-file torrent yields the right file list and total_length;
    //   - a magnet with a hex hash and a magnet with a base32 hash both parse to the
    //     same infohash; `check_consistency` rejects a doctored piece count.
}
