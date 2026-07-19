//! V2 — Inter-SFU cascade transport. `src/cascade.rs`.
//!
//! This is the heart of "cascaded": the transport that makes an SFU a **peer of another SFU**. A
//! publisher's origin media lives in its home region (V1). A subscriber in a *different* region
//! must receive it — but the origin SFU must **not** send one copy per remote subscriber across
//! the ocean. Instead it opens **one relay leg per remote region with interest** and sends a
//! **single** copy of each forwarded stream down that leg; the remote SFU receives it and does the
//! **local** fan-out (reusing project 15's per-subscriber [`Rewriter`] for its own locals). Inter-
//! region cost becomes `O(regions)`, not `O(subscribers)` — the whole win.
//!
//! The subtleties are all about being a relay **without a loop**: a relay copy arriving from region
//! `A` is fanned out to local subscribers but is **never** relayed onward (to `A` or a third
//! region), or a 3-region mesh forwards a packet in circles forever. So a relayed packet carries
//! provenance, the SFU relays **only origin media** and fans out **relay media locally**, and legs
//! are **bounded** (a fixed peer set, one leg each, torn down when the last remote subscriber
//! leaves).
//!
//! Scaffold state: [`CascadeMesh::new`] and the read-only [`links`](CascadeMesh::links) snapshot are
//! wired so `/status` shows the (empty) leg table. Opening a leg, relaying a copy out, and fanning a
//! relayed copy in are the V2 `todo!()` worklist. The backbone UDP socket is bound in `main` and
//! handed to [`run`](CascadeMesh::run).

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use serde::Serialize;
use tokio::net::UdpSocket;

use crate::error::Result;
use crate::placement::PeerNode;

/// One backbone relay leg: a live SFU→SFU link carrying origin media to a peer region that has
/// interest. Exists only while that region has ≥1 subscriber for a stream here.
#[derive(Clone, Debug, Serialize)]
pub struct RelayLink {
    /// The peer region this leg feeds.
    pub region: String,
    /// Where relay copies are sent (the peer's cascade UDP address).
    pub remote_addr: String,
    /// How many distinct origin streams (publisher tracks) are currently relayed on this leg.
    pub tracks: usize,
}

/// A relayed datagram to send on the backbone socket (analogous to project 15's `Outgoing`).
#[derive(Debug)]
pub struct RelayOut {
    pub dst: SocketAddr,
    pub data: Bytes,
}

/// Config for the cascade mesh, read from env in `main`.
pub struct CascadeConfig {
    /// This SFU's region — the loop guard: a relay copy is never sent back to, or fanned out as if
    /// from, this region, and origin media from here is relayed out but never re-relayed.
    pub region: String,
    /// The backbone UDP port this SFU relays on.
    pub cascade_port: u16,
    /// The peer SFUs (region → cascade address) legs can be opened to.
    pub peers: Vec<PeerNode>,
    /// Cap on concurrently-open relay legs (abuse/backpressure bound).
    pub max_links: usize,
}

/// Mutable leg table, guarded by one lock (sync work only; the socket `send_to` awaits happen in
/// `run`/the caller after the lock is dropped — the p15 locking discipline).
#[derive(Default)]
struct Inner {
    /// Open relay legs keyed by peer region.
    links: HashMap<String, RelayLink>,
}

/// The cascade mesh: the set of relay legs to peer regions + the backbone transport.
pub struct CascadeMesh {
    cfg: CascadeConfig,
    inner: Mutex<Inner>,
}

impl CascadeMesh {
    /// Build the mesh. Wiring only — legs open on demand (V2), not here.
    pub fn new(cfg: CascadeConfig) -> Self {
        Self {
            cfg,
            inner: Mutex::new(Inner::default()),
        }
    }

    pub fn config(&self) -> &CascadeConfig {
        &self.cfg
    }

    /// This SFU's region (the loop guard).
    pub fn region(&self) -> &str {
        &self.cfg.region
    }

    /// A snapshot of the open relay legs (for `/status`).
    pub fn links(&self) -> Vec<RelayLink> {
        self.inner
            .lock()
            .expect("cascade lock")
            .links
            .values()
            .cloned()
            .collect()
    }

    /// The peer node for a region, if it's in the mesh (used to authenticate a relay source and to
    /// resolve where a leg sends). Wired lookup — no I/O.
    pub fn peer(&self, region: &str) -> Option<&PeerNode> {
        self.cfg.peers.iter().find(|p| p.region == region)
    }

    // ---- V2 worklist: open legs · relay out · fan in (loop-free) --------------------------

    /// TODO(V2): Ensure a relay leg to `region` exists for `stream` (idempotent). Opens the leg on
    /// the first remote subscriber for a stream there, bumps its track count, enforces
    /// `max_links`. Returns the leg. Called from signaling when a subscriber's publisher is in
    /// another region.
    pub async fn ensure_link(&self, region: &str, stream: &str) -> Result<RelayLink> {
        let _ = (region, stream, self.cfg.max_links);
        todo!("V2: open-or-reuse the relay leg to this region, enforce MAX_RELAY_LINKS")
    }

    /// TODO(V2): Tear down interest in `stream` on the leg to `region`; close the leg when its last
    /// track's last remote subscriber leaves. Idempotent (safe on an already-closed leg).
    pub async fn release_link(&self, region: &str, stream: &str) -> Result<()> {
        let _ = (region, stream);
        todo!("V2: decrement leg interest, close the leg when it reaches zero")
    }

    /// TODO(V2): Relay **one** copy of an origin packet for `stream` to each remote region with
    /// interest — one `send_to` per leg, regardless of how many subscribers that region has (it
    /// fans out locally). **Loop guard:** only *origin* media (produced in this region) is relayed;
    /// never re-relay a packet that arrived as a relay copy. Bumps `RELAY_COPIES_OUT` per region.
    pub async fn relay_out(&self, stream: &str, packet: &[u8]) -> Result<Vec<RelayOut>> {
        let _ = (stream, packet, self.cfg.region.as_str());
        todo!("V2: send one relay copy per interested remote region (origin media only)")
    }

    /// TODO(V2): A relay copy arrived from a peer SFU (`from`). Authenticate the source (must be a
    /// known peer region), then hand it to the **local** fan-out (project 15's rewriter, per local
    /// subscriber). **Loop guard:** a relayed copy is fanned out locally and **never** re-relayed
    /// onward. Bumps `RELAY_COPIES_IN`; drops unknown/looping/truncated packets by reason.
    pub async fn on_relayed(&self, from: SocketAddr, packet: &[u8]) -> Result<()> {
        let _ = (from, packet);
        todo!("V2: authenticate peer, fan out locally, never re-relay (loop-free)")
    }

    /// TODO(V2): The backbone pump — receive relay copies on the cascade socket and dispatch them
    /// to [`on_relayed`](Self::on_relayed), draining outbound relay copies via `socket.send_to`.
    /// Runs until `shutdown`. Gated behind `RUN_BACKGROUND` in `main` (a bare scaffold with no
    /// peers has nothing to relay).
    pub async fn run(
        self: Arc<Self>,
        socket: Arc<UdpSocket>,
        mut shutdown: tokio::sync::watch::Receiver<bool>,
    ) -> Result<()> {
        let _ = &socket;
        let _ = shutdown.changed().await;
        todo!("V2: recv relay copies -> on_relayed; send outbound relay copies; drain on shutdown")
    }
}
