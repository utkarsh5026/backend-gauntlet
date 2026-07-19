//! V5 — Piece selection & verification: assembling a file from strangers.
//!
//! This is the leech loop. You have peers (V3), a wire to talk to them (V4), and a list
//! of piece hashes (V2). Now: decide *which* piece to fetch, split it into ≤ 16 KiB
//! block `request`s across the peers that have it, reassemble, and — the crux —
//! **verify the piece's SHA-1 against the metainfo hash before it counts**. You are
//! building a file out of bytes handed to you by anonymous strangers; verify-before-
//! write is the trust boundary. A piece that fails the hash is discarded and refetched.
//!
//! Two scheduling ideas make the swarm healthy instead of naive:
//!   - **Rarest-first**: pick the piece the *fewest* peers have, from their bitfields —
//!     so no piece goes extinct and the swarm's rarest blocks spread fastest. (Naive
//!     sequential download starves the swarm and yourself.)
//!   - **Endgame**: for the last few missing blocks, request them from *several* peers
//!     at once and cancel the losers — trading a little duplicate bandwidth to avoid
//!     stalling on one slow peer at 99%.

use std::path::{Path, PathBuf};

use crate::error::AppError;
use crate::metainfo::Metainfo;

/// Blocks are requested in ≤ 16 KiB chunks (matches [`crate::peer::BLOCK_SIZE`]).
pub const BLOCK_SIZE: u32 = 16 * 1024;

/// The on-disk backing store for one torrent's data, plus the have-bitfield. Both the
/// download loop (writes verified pieces) and the seeder (reads pieces to serve) go
/// through this, so it owns the file layout and the "do I have piece i?" answer.
pub struct PieceStore {
    dir: PathBuf,
    piece_length: u64,
    total_length: u64,
    /// `have[i]` = piece `i` is present *and verified*.
    have: Vec<bool>,
}

impl PieceStore {
    /// Open (or create) the store for a torrent under `dir`.
    ///
    /// TODO(V5): lay out the file(s), pre-size them (sparse is fine), and build the
    /// `have` bitfield — on a fresh start it's all `false`; on a resume, re-hash the
    /// existing pieces so already-complete work isn't refetched (the resume criterion).
    pub fn create(meta: &Metainfo, dir: &Path) -> Result<Self, AppError> {
        let _ = (meta, dir);
        todo!("V5: allocate the file(s) and build the have-bitfield (rescan on resume)")
    }

    /// Has piece `index` been downloaded *and verified*?
    pub fn has_piece(&self, index: usize) -> bool {
        self.have.get(index).copied().unwrap_or(false)
    }

    /// How many pieces are complete — drives progress reporting.
    pub fn have_count(&self) -> usize {
        self.have.iter().filter(|h| **h).count()
    }

    /// Persist a piece that has **already passed** [`verify_piece`], and mark it have.
    ///
    /// TODO(V5): write `data` at the piece's byte offset (spanning file boundaries for a
    /// multi-file torrent), flip `have[index]`, and note it so a `have` message goes out
    /// to peers. Callers must verify first — this method trusts its input.
    pub fn write_verified_piece(&mut self, index: usize, data: &[u8]) -> Result<(), AppError> {
        let _ = (index, data);
        let _ = (self.piece_length, self.total_length, &self.dir);
        todo!("V5: write a verified piece to disk and mark it have")
    }

    /// Read `length` bytes at `(index, begin)` to answer a peer's `request` (used by V6).
    ///
    /// TODO(V5): bounds-check against the piece/file sizes and read the slice. Refuse a
    /// request for a piece we don't have, or one that runs off the end.
    pub fn read_block(&self, index: usize, begin: u32, length: u32) -> Result<Vec<u8>, AppError> {
        let _ = (index, begin, length);
        todo!("V5: read a block for the seeder to serve (bounds-checked)")
    }
}

/// Does `data` hash to the expected piece SHA-1? This one line is the trust boundary.
///
/// TODO(V5): `Sha1::digest(data)` (from the `sha1` crate) and compare to `expected` in
/// full. Return `false` on mismatch — the caller discards the block and refetches.
pub fn verify_piece(expected: &[u8; 20], data: &[u8]) -> bool {
    let _ = (expected, data);
    todo!("V5: SHA-1 the piece bytes and compare to the metainfo hash")
}

/// Choose the next piece to download, rarest-first.
///
/// TODO(V5): among pieces we don't yet have, pick one with the lowest `availability`
/// (how many connected peers advertise it), breaking ties randomly so parallel
/// downloaders don't all grab the same piece. Return `None` when nothing is left.
pub fn pick_piece(local_have: &[bool], availability: &[u16]) -> Option<usize> {
    let _ = (local_have, availability);
    todo!("V5: rarest-first piece selection (lowest availability we don't have)")
}

/// Drive the download of one torrent to completion.
///
/// TODO(V5): the loop that ties it together — maintain per-piece availability from
/// peers' bitfields, pick pieces ([`pick_piece`]), pipeline block `request`s (several
/// in flight per peer, not one-per-RTT) to unchoked peers, reassemble each piece,
/// [`verify_piece`] it, [`PieceStore::write_verified_piece`], broadcast `have`, and
/// switch to endgame for the tail. Return once every piece is verified.
pub async fn run_download(meta: &Metainfo, store: &mut PieceStore) -> Result<(), AppError> {
    let _ = (meta, store);
    todo!("V5: drive the leech loop to completion (pick → request → verify → write)")
}

#[cfg(test)]
mod tests {
    // TODO(V5): prove correctness + strategy.
    //   - end-to-end (needs `docker compose up`): leech a small torrent from the
    //     reference seed and assert the output file's SHA-256 == the source's;
    //   - fault injection: feed one corrupted block; assert `verify_piece` rejects it,
    //     the piece is refetched, and the final file is still correct — a lying peer
    //     cannot corrupt the output;
    //   - `pick_piece` returns the rarest missing piece and never one we already have;
    //   - resume: with N pieces already on disk, a fresh `PieceStore::create` reports
    //     them as have and the loop doesn't refetch them.
}
