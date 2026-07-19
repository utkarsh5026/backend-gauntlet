//! The SFU core — **wired** shared state that ties the verticals together, the moral
//! equivalent of project 14's `session.rs`.
//!
//! It owns the room/peer graph and, per peer, the vertical state objects: an [`IceAgent`]
//! (V1), a per-subscriber [`Rewriter`] (V2) + [`LayerSelector`] (V3), and a
//! [`BandwidthEstimator`] (V4). The signaling handlers ([`crate::signaling`]) call the
//! `join_*` / `subscribe` ops to build the graph; the UDP [`pump`](crate::pump) calls the
//! `handle_*` media-plane ops for each datagram. Those media ops do the wired lookup/fan-out
//! and call into the vertical primitives — whose bodies are the `todo!()`s. So this compiles
//! and runs: with no clients it idles; the first STUN check a real browser sends hits the V1
//! `StunMessage::parse` `todo!()` — that panic is your worklist.
//!
//! Locking rule: every method takes the inner `Mutex`, does purely synchronous work, and
//! returns a list of [`Outgoing`] datagrams. The pump performs the actual `send_to` awaits
//! *after* dropping the lock — the lock is never held across `.await`.

use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::sync::Mutex;

use bytes::Bytes;
use tracing::{debug, info};

use crate::bwe::BandwidthEstimator;
use crate::error::{Result, SfuError};
use crate::forward::Rewriter;
use crate::ice::{IceAction, IceAgent, StunMessage};
use crate::metrics::{
    BYTES_FORWARDED, ESTIMATED_BITRATE, ICE_NOMINATED, KEYFRAME_REQUESTS, PEERS, ROOMS,
    RTP_DROPPED, RTP_FORWARDED, RTP_RECEIVED, STUN_MESSAGES,
};
use crate::simulcast::{Decision, LayerSelector, SimulcastLayer};
use crate::wire::RtpView;

pub type RoomId = String;
pub type PeerId = u64;

/// A datagram the pump should send on the media socket.
#[derive(Debug)]
pub struct Outgoing {
    pub dst: SocketAddr,
    pub data: Bytes,
}

/// Immutable SFU configuration, shared (behind an `Arc`) by the core + admin server.
#[derive(Debug, Clone)]
pub struct SfuConfig {
    /// Host candidate address advertised to clients in signaling.
    pub public_ip: IpAddr,
    pub media_port: u16,
    pub max_rooms: usize,
    pub max_peers_per_room: usize,
    /// Per-subscriber estimator bounds (bits/sec).
    pub min_bitrate: u32,
    pub start_bitrate: u32,
    pub max_bitrate: u32,
}

impl SfuConfig {
    /// The ICE host candidate the SFU advertises (where clients send their checks + media).
    pub fn media_addr(&self) -> SocketAddr {
        SocketAddr::new(self.public_ip, self.media_port)
    }
}

/// One connected peer and its per-role vertical state.
struct Peer {
    id: PeerId,
    room: RoomId,
    role: Role,
    ice: IceAgent,
    /// The peer's local ICE credentials (the SFU side of the exchange).
    local_ufrag: String,
    /// Publisher: the simulcast layers it announced (empty for a subscriber).
    layers: Vec<SimulcastLayer>,
    /// Subscriber: which publisher it watches, plus its rewriter + selector (None for a publisher).
    subscribes_to: Option<PeerId>,
    rewriter: Option<Rewriter>,
    selector: Option<LayerSelector>,
    bwe: BandwidthEstimator,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Publisher,
    Subscriber,
}

struct Room {
    peers: Vec<PeerId>,
}

#[derive(Default)]
struct Inner {
    rooms: HashMap<RoomId, Room>,
    peers: HashMap<PeerId, Peer>,
    /// Local ICE ufrag → peer, for routing an inbound STUN check before a pair is nominated.
    by_ufrag: HashMap<String, PeerId>,
    /// Nominated media source address → peer, for routing RTP/RTCP once ICE completes.
    by_addr: HashMap<SocketAddr, PeerId>,
    /// Publisher layer SSRC → publisher peer, for routing an inbound RTP packet to its origin.
    by_ssrc: HashMap<u32, PeerId>,
    next_peer: PeerId,
}

/// The credentials + address handed back to a client from signaling so it can ICE-connect.
#[derive(Debug, serde::Serialize)]
pub struct PeerHandle {
    pub peer_id: PeerId,
    pub ice_ufrag: String,
    pub ice_pwd: String,
    pub media_addr: String,
    /// For a subscriber: the stable SSRC it will receive on.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub out_ssrc: Option<u32>,
}

/// The shared SFU. `Arc<Sfu>` is cloned into the pump task and the signaling router.
pub struct Sfu {
    cfg: SfuConfig,
    inner: Mutex<Inner>,
}

impl Sfu {
    pub fn new(cfg: SfuConfig) -> Self {
        Self {
            cfg,
            inner: Mutex::new(Inner::default()),
        }
    }

    pub fn config(&self) -> &SfuConfig {
        &self.cfg
    }

    // ----------------------------------------------------------------------------------
    // Signaling plane (wired) — build the room/peer graph. Called from HTTP handlers.
    // ----------------------------------------------------------------------------------

    /// Register a **publisher** announcing its simulcast layers; returns its ICE credentials.
    pub fn join_publisher(
        &self,
        room: &str,
        layers: Vec<SimulcastLayer>,
        client_ufrag: String,
    ) -> Result<PeerHandle> {
        let mut inner = self.inner.lock().unwrap();
        self.ensure_room(&mut inner, room)?;
        let (local_ufrag, local_pwd) = gen_credentials();
        let id = inner.next_peer;
        inner.next_peer += 1;

        for layer in &layers {
            inner.by_ssrc.insert(layer.ssrc, id);
        }
        let peer = Peer {
            id,
            room: room.to_string(),
            role: Role::Publisher,
            ice: IceAgent::new(local_ufrag.clone(), local_pwd.clone(), client_ufrag),
            local_ufrag: local_ufrag.clone(),
            layers,
            subscribes_to: None,
            rewriter: None,
            selector: None,
            bwe: BandwidthEstimator::new(
                self.cfg.start_bitrate,
                self.cfg.min_bitrate,
                self.cfg.max_bitrate,
            ),
        };
        self.insert_peer(&mut inner, peer);
        info!(room, peer = id, "publisher joined");
        Ok(PeerHandle {
            peer_id: id,
            ice_ufrag: local_ufrag,
            ice_pwd: local_pwd,
            media_addr: self.cfg.media_addr().to_string(),
            out_ssrc: None,
        })
    }

    /// Register a **subscriber** watching `publisher`; returns its ICE credentials + the
    /// stable SSRC it will receive on. Wires up its rewriter (V2) + layer selector (V3).
    pub fn subscribe(
        &self,
        room: &str,
        publisher: PeerId,
        client_ufrag: String,
    ) -> Result<PeerHandle> {
        let mut inner = self.inner.lock().unwrap();
        let layers = match inner.peers.get(&publisher) {
            Some(p) if p.role == Role::Publisher => p.layers.clone(),
            _ => return Err(SfuError::NotFound(format!("publisher {publisher}"))),
        };
        self.ensure_room(&mut inner, room)?;
        let (local_ufrag, local_pwd) = gen_credentials();
        let out_ssrc = rand::random();
        let id = inner.next_peer;
        inner.next_peer += 1;

        let peer = Peer {
            id,
            room: room.to_string(),
            role: Role::Subscriber,
            ice: IceAgent::new(local_ufrag.clone(), local_pwd.clone(), client_ufrag),
            local_ufrag: local_ufrag.clone(),
            layers: Vec::new(),
            subscribes_to: Some(publisher),
            rewriter: Some(Rewriter::new(out_ssrc)),
            selector: Some(LayerSelector::new(layers)),
            bwe: BandwidthEstimator::new(
                self.cfg.start_bitrate,
                self.cfg.min_bitrate,
                self.cfg.max_bitrate,
            ),
        };
        self.insert_peer(&mut inner, peer);
        info!(room, peer = id, publisher, "subscriber joined");
        Ok(PeerHandle {
            peer_id: id,
            ice_ufrag: local_ufrag,
            ice_pwd: local_pwd,
            media_addr: self.cfg.media_addr().to_string(),
            out_ssrc: Some(out_ssrc),
        })
    }

    /// A JSON-friendly snapshot of the topology for `GET /rooms` and `/status`.
    pub fn topology(&self) -> serde_json::Value {
        let inner = self.inner.lock().unwrap();
        let rooms: Vec<_> = inner
            .rooms
            .iter()
            .map(|(id, r)| {
                serde_json::json!({
                    "room": id,
                    "peers": r.peers.iter().filter_map(|pid| inner.peers.get(pid)).map(|p| {
                        serde_json::json!({ "id": p.id, "role": format!("{:?}", p.role) })
                    }).collect::<Vec<_>>(),
                })
            })
            .collect();
        serde_json::json!({ "rooms": rooms })
    }

    fn ensure_room(&self, inner: &mut Inner, room: &str) -> Result<()> {
        if !inner.rooms.contains_key(room) {
            if inner.rooms.len() >= self.cfg.max_rooms {
                return Err(SfuError::Rejected(format!(
                    "max rooms ({}) reached",
                    self.cfg.max_rooms
                )));
            }
            inner
                .rooms
                .insert(room.to_string(), Room { peers: Vec::new() });
            metrics::gauge!(ROOMS).set(inner.rooms.len() as f64);
        }
        let count = inner.rooms[room].peers.len();
        if count >= self.cfg.max_peers_per_room {
            return Err(SfuError::Rejected(format!(
                "room {room} full ({} peers)",
                self.cfg.max_peers_per_room
            )));
        }
        Ok(())
    }

    fn insert_peer(&self, inner: &mut Inner, peer: Peer) {
        let (id, room, ufrag, role) = (
            peer.id,
            peer.room.clone(),
            peer.local_ufrag.clone(),
            peer.role,
        );
        inner.by_ufrag.insert(ufrag, id);
        inner
            .rooms
            .entry(room)
            .or_insert_with(|| Room { peers: Vec::new() })
            .peers
            .push(id);
        inner.peers.insert(id, peer);
        let role_label = if role == Role::Publisher {
            "publisher"
        } else {
            "subscriber"
        };
        metrics::gauge!(PEERS, "role" => role_label).increment(1.0);
    }

    // ----------------------------------------------------------------------------------
    // Media plane (wired dispatch → vertical primitives). Called from the UDP pump.
    // ----------------------------------------------------------------------------------

    /// Handle an inbound **STUN** datagram (ICE connectivity check). Reaches V1.
    pub fn handle_stun(&self, from: SocketAddr, buf: &[u8]) -> Result<Vec<Outgoing>> {
        // First todo on the media path: parsing the STUN message (V1).
        let msg = StunMessage::parse(buf)?;
        metrics::counter!(STUN_MESSAGES, "kind" => "request").increment(1);

        // Route the check to a peer by the local ufrag in its USERNAME (`<local>:<remote>`).
        let mut inner = self.inner.lock().unwrap();
        let peer_id = username_local_ufrag(&msg)
            .and_then(|u| inner.by_ufrag.get(&u).copied())
            .ok_or_else(|| SfuError::NotFound("no peer for STUN username".into()))?;

        let action = {
            let peer = inner.peers.get_mut(&peer_id).unwrap();
            peer.ice.handle(&msg, from)? // V1
        };
        match action {
            IceAction::Respond(data) => Ok(vec![Outgoing {
                dst: from,
                data: data.into(),
            }]),
            IceAction::Nominated { peer } => {
                inner.by_addr.insert(peer, peer_id);
                metrics::counter!(ICE_NOMINATED).increment(1);
                info!(peer = peer_id, %from, "ICE pair nominated");
                Ok(Vec::new())
            }
            IceAction::Nothing => Ok(Vec::new()),
        }
    }

    /// Handle an inbound **RTP** datagram from a publisher: fan it out to that publisher's
    /// subscribers, each through its selector (V3) + rewriter (V2). Returns the rewritten
    /// datagrams to send.
    pub fn handle_rtp(&self, from: SocketAddr, buf: &[u8]) -> Result<Vec<Outgoing>> {
        let mut inner = self.inner.lock().unwrap();
        metrics::counter!(RTP_RECEIVED).increment(1);

        // The source must be a nominated publisher (ICE done); otherwise drop — an open port
        // takes bytes from anyone.
        let Some(&pub_id) = inner.by_addr.get(&from) else {
            metrics::counter!(RTP_DROPPED, "reason" => "no_route").increment(1);
            return Ok(Vec::new());
        };
        if buf.len() < crate::wire::RTP_MIN_HEADER {
            return Err(SfuError::Truncated {
                need: crate::wire::RTP_MIN_HEADER,
                got: buf.len(),
            });
        }
        let origin_ssrc = u32::from_be_bytes([buf[8], buf[9], buf[10], buf[11]]);
        // Crude keyframe heuristic for the switch boundary (assumes no CSRC/extension); V3
        // refines real H.264/VP8 keyframe detection.
        let is_keyframe = buf
            .get(crate::wire::RTP_MIN_HEADER)
            .map(|b| b & 0x1f == 5)
            .unwrap_or(false);

        // Which subscribers watch this publisher?
        let subs: Vec<PeerId> = inner
            .peers
            .values()
            .filter(|p| p.subscribes_to == Some(pub_id))
            .map(|p| p.id)
            .collect();

        let mut out = Vec::new();
        let mut keyframe_requests: Vec<PeerId> = Vec::new();
        for sub_id in subs {
            let peer = inner.peers.get_mut(&sub_id).unwrap();
            let (selector, rewriter) = match (peer.selector.as_mut(), peer.rewriter.as_mut()) {
                (Some(s), Some(r)) => (s, r),
                _ => continue,
            };
            match selector.on_packet(origin_ssrc, is_keyframe) {
                // V3
                Decision::Forward => {
                    let mut copy = buf.to_vec();
                    if let Some(mut view) = RtpView::new(&mut copy) {
                        rewriter.rewrite(&mut view); // V2
                    }
                    if let Some(dst) = peer.ice.peer() {
                        metrics::counter!(RTP_FORWARDED).increment(1);
                        metrics::counter!(BYTES_FORWARDED).increment(copy.len() as u64);
                        out.push(Outgoing {
                            dst,
                            data: copy.into(),
                        });
                    }
                }
                Decision::Drop => {
                    rewriter.skip(); // V2
                    metrics::counter!(RTP_DROPPED, "reason" => "not_selected").increment(1);
                }
            }
            if selector.wants_keyframe() {
                keyframe_requests.push(pub_id);
            }
        }

        // Ask the publisher for a keyframe if any subscriber's up-switch is waiting on one.
        if !keyframe_requests.is_empty() {
            if let Some(dst) = inner.peers.get(&pub_id).and_then(|p| p.ice.peer()) {
                metrics::counter!(KEYFRAME_REQUESTS).increment(1);
                debug!(publisher = pub_id, "would send PLI/FIR upstream");
                // A real PLI (PSFB, fmt 1) datagram would be built + queued here.
                let _ = dst;
            }
        }
        Ok(out)
    }

    /// Handle an inbound **RTCP** datagram (feedback). Drives the bandwidth estimate (V4) and,
    /// for a NACK, feeds the rewriter's NACK translation (V2). Reaches V4 on a receiver report.
    pub fn handle_rtcp(&self, from: SocketAddr, buf: &[u8]) -> Result<Vec<Outgoing>> {
        if buf.len() < 8 {
            return Err(SfuError::Truncated {
                need: 8,
                got: buf.len(),
            });
        }
        let mut inner = self.inner.lock().unwrap();
        let Some(&peer_id) = inner.by_addr.get(&from) else {
            return Ok(Vec::new());
        };
        let pt = buf[1];
        // Receiver Report (201): the fraction-lost byte sits at offset 12 of the first report
        // block — the loss-based BWE signal (V4). A fuller RTCP compound parser (RR/NACK/TWCC/
        // REMB) is part of the reliability + observability horizontal work.
        if pt == 201 && buf.len() >= 13 {
            let fraction_lost = buf[12] as f64 / 256.0;
            if let Some(peer) = inner.peers.get_mut(&peer_id) {
                let est = peer.bwe.on_loss(fraction_lost); // V4
                metrics::gauge!(ESTIMATED_BITRATE).set(est as f64);
                if let Some(sel) = peer.selector.as_mut() {
                    sel.set_budget(est); // V3 consumes the estimate
                }
            }
        }
        Ok(Vec::new())
    }
}

/// Generate a fresh ICE ufrag + pwd (RFC 5245 wants ≥ 4 / ≥ 22 chars of ICE-allowed text).
fn gen_credentials() -> (String, String) {
    let ufrag = format!("{:08x}", rand::random::<u32>());
    let pwd = format!(
        "{:016x}{:016x}",
        rand::random::<u64>(),
        rand::random::<u64>()
    );
    (ufrag, pwd)
}

/// Extract the local (SFU-side) ufrag from a STUN USERNAME attribute (`<local>:<remote>`).
fn username_local_ufrag(msg: &StunMessage) -> Option<String> {
    msg.attributes.iter().find_map(|a| match a {
        crate::ice::StunAttribute::Username(u) => u.split(':').next().map(|s| s.to_string()),
        _ => None,
    })
}
