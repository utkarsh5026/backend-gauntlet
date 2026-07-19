//! The engine that ties the verticals together — mostly wiring, so the interesting
//! logic stays in the vertical modules it calls.
//!
//! [`Client`] holds the run-wide config (peer id, listen port, download dir, caps) and a
//! registry of managed torrents. [`Client::add_torrent`] parses a source into an
//! infohash (V2) and records it; the control plane ([`crate::routes`]) reads the
//! registry for `GET /torrents`. Driving each torrent's tracker/peer/download tasks is
//! left as a TODO you fill in as V3–V6 come online.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde::Serialize;

use crate::error::AppError;
use crate::metainfo::{MagnetLink, Metainfo};
use crate::types::{InfoHash, PeerId};

/// Run-wide configuration, built once in `main` from the environment.
#[derive(Debug, Clone)]
pub struct ClientConfig {
    pub peer_id: PeerId,
    /// Port we listen on for inbound peers (advertised to the tracker).
    pub peer_port: u16,
    pub download_dir: PathBuf,
    /// Cap on total simultaneous peer connections (resource bound).
    pub max_peers: usize,
    /// Regular upload slots for the seeder's choke algorithm (V6).
    pub upload_slots: usize,
}

/// How a torrent was added.
pub enum TorrentSource {
    /// Raw `.torrent` file bytes.
    Torrent(Vec<u8>),
    /// A `magnet:` URI.
    Magnet(String),
}

/// A point-in-time view of a managed torrent — what `GET /torrents` renders.
#[derive(Debug, Clone, Serialize)]
pub struct TorrentStatus {
    pub info_hash: InfoHash,
    pub name: String,
    pub total_length: u64,
    pub downloaded: u64,
    pub uploaded: u64,
    pub peers: usize,
    pub have_pieces: usize,
    pub total_pieces: usize,
}

/// The client engine. Cheap to clone (it's always behind an `Arc`).
pub struct Client {
    cfg: ClientConfig,
    torrents: Mutex<HashMap<InfoHash, TorrentStatus>>,
}

impl Client {
    pub fn new(cfg: ClientConfig) -> Arc<Self> {
        Arc::new(Self {
            cfg,
            torrents: Mutex::new(HashMap::new()),
        })
    }

    pub fn config(&self) -> &ClientConfig {
        &self.cfg
    }

    /// Add a torrent from a `.torrent` or magnet, returning its infohash.
    ///
    /// Wired: parses the source (V2) and records the torrent so it shows up in
    /// `GET /torrents`. In the scaffold this `todo!()`-panics inside [`Metainfo::from_bytes`]
    /// / [`MagnetLink::parse`] the moment it's called — V2 is where the worklist starts.
    ///
    /// TODO(engine): once V2–V6 exist, spawn this torrent's drive tasks here — announce
    /// to its trackers (V3), dial + handshake peers (V4), run the piece loop (V5), and
    /// register it with the seeder (V6). Track live progress back into `TorrentStatus`.
    pub async fn add_torrent(&self, source: TorrentSource) -> Result<InfoHash, AppError> {
        let status = match source {
            TorrentSource::Torrent(bytes) => {
                let meta = Metainfo::from_bytes(&bytes)?;
                TorrentStatus {
                    info_hash: meta.info_hash,
                    name: meta.name.clone(),
                    total_length: meta.total_length,
                    downloaded: 0,
                    uploaded: 0,
                    peers: 0,
                    have_pieces: 0,
                    total_pieces: meta.piece_count(),
                }
            }
            TorrentSource::Magnet(uri) => {
                let magnet = MagnetLink::parse(&uri)?;
                let info_hash = magnet.info_hash;
                TorrentStatus {
                    info_hash,
                    // A fresh magnet has no metainfo yet (fetched from peers later), so
                    // fall back to the hex id until the name is known.
                    name: magnet.name.unwrap_or_else(|| info_hash.to_hex()),
                    total_length: 0,
                    downloaded: 0,
                    uploaded: 0,
                    peers: 0,
                    have_pieces: 0,
                    total_pieces: 0,
                }
            }
        };

        let info_hash = status.info_hash;
        self.torrents.lock().unwrap().insert(info_hash, status);
        Ok(info_hash)
    }

    /// Every managed torrent's current status.
    pub fn status(&self) -> Vec<TorrentStatus> {
        self.torrents.lock().unwrap().values().cloned().collect()
    }

    /// One torrent's status, if managed.
    pub fn get(&self, info_hash: &InfoHash) -> Option<TorrentStatus> {
        self.torrents.lock().unwrap().get(info_hash).cloned()
    }
}

/// Mint this run's peer id: an 8-byte client prefix (`-RB0001-`, Azureus style) + 12
/// random bytes. Identity plumbing — the *protocol* is the learning, not this.
pub fn generate_peer_id() -> PeerId {
    let mut id = [0u8; 20];
    id[..8].copy_from_slice(b"-RB0001-");
    for b in id[8..].iter_mut() {
        *b = rand::random();
    }
    PeerId(id)
}
