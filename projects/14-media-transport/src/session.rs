//! The transport sessions — **wired** loops that tie the verticals together over one UDP
//! socket. Two roles run the same socket in opposite directions:
//!
//! - [`run_sender`]: pull a frame → **packetize** (V1) → **pace** (V4) → `send_to`; on RTCP
//!   feedback, **retransmit** NACK'd packets (V3) and update the estimate (V4).
//! - [`run_receiver`]: `recv_from` → **parse** (V1) → **jitter buffer** (V2); on a playout
//!   tick, release + **depacketize** (V1) complete frames; periodically **NACK** the gaps (V3).
//!
//! The loops (the `select!`, the timers, the socket I/O, the metrics) are done — the calls
//! they make into `rtp`/`jitter`/`rtcp`/`congestion` are the `todo!()`s. So the scaffold
//! runs: as `ROLE=receiver` it binds and idles until a datagram arrives; as `ROLE=sender` it
//! produces a frame and hits the V1 packetize `todo!()` — that panic is your worklist.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Context;
use tokio::net::UdpSocket;
use tokio::sync::watch;
use tokio::time::interval;
use tracing::info;

use crate::congestion::CongestionController;
use crate::jitter::JitterBuffer;
// Metric-name constants, imported bare so the `metrics::` prefix stays unambiguously the
// external `metrics` crate (which owns the `counter!`/`gauge!` macros).
use crate::media::SyntheticSource;
use crate::metrics::{
    BYTES_RECEIVED, BYTES_SENT, JITTER_BUFFER_DEPTH, NACKS_TOTAL, PACKETS_LOST, PACKETS_RECEIVED,
    PACKETS_SENT, RETRANSMITS, TARGET_BITRATE,
};
use crate::rtcp::{Nack, RetransmitCache, RtcpPacket};
use crate::rtp::{depacketize, Packetizer, RtpPacket, H264_CLOCK_RATE};

/// How many recently sent packets the sender keeps for retransmission (V3). Also the
/// staleness bound: older than this window and a NACK goes unanswered.
const RTX_CACHE_PACKETS: usize = 1024;
/// Hard cap on jitter-buffer occupancy (V2) — the OOM guard against a future-sequence flood.
const JITTER_CAPACITY: usize = 4096;
/// How often the receiver checks for playable frames.
const PLAYOUT_TICK_MS: u64 = 10;
/// How often the receiver emits RTCP feedback (NACK / receiver report).
const FEEDBACK_INTERVAL_MS: u64 = 200;

/// Which direction this process runs the socket.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Sender,
    Receiver,
}

/// Immutable transport configuration, shared (behind an `Arc`) by the session + admin server.
#[derive(Debug, Clone)]
pub struct TransportConfig {
    pub role: Role,
    /// Where the sender sends (required for `Role::Sender`).
    pub remote_addr: Option<SocketAddr>,
    /// Max UDP payload per packet (header + media).
    pub mtu: usize,
    pub payload_type: u8,
    /// Jitter-buffer target playout delay.
    pub target_playout: Duration,
    /// Congestion-control bitrate bounds (bits/sec).
    pub start_bitrate: u32,
    pub min_bitrate: u32,
    pub max_bitrate: u32,
    pub fps: u32,
    pub gop: u32,
}

/// Run the **sender** side until shutdown (wired; V1/V3/V4 plug in).
pub async fn run_sender(
    socket: Arc<UdpSocket>,
    cfg: Arc<TransportConfig>,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let remote = cfg
        .remote_addr
        .context("ROLE=sender requires REMOTE_ADDR (host:port of the receiver)")?;

    let ssrc: u32 = rand::random();
    let initial_seq: u16 = rand::random();
    let mut packetizer = Packetizer::new(ssrc, cfg.payload_type, cfg.mtu, initial_seq);
    let mut cc = CongestionController::new(cfg.start_bitrate, cfg.min_bitrate, cfg.max_bitrate);
    let mut rtx = RetransmitCache::new(RTX_CACHE_PACKETS);
    let mut source = SyntheticSource::new(cfg.fps, cc.target_bitrate() / 1000, cfg.gop);
    let mut rtcp_buf = vec![0u8; 1500];

    metrics::gauge!(TARGET_BITRATE).set(cc.target_bitrate() as f64);
    info!(%remote, ssrc, "sender started");

    loop {
        tokio::select! {
            frame = source.next_frame() => {
                // V1: split the access unit into RTP packets (first todo!() a sender hits).
                let packets = packetizer.packetize(&frame.data, frame.rtp_timestamp)?;
                for p in packets {
                    let bytes = p.serialize();                       // V1
                    // V4: the pacer gates the send; wired here so the estimator drives it.
                    let _ = cc.can_send(Instant::now(), bytes.len());
                    socket.send_to(&bytes, remote).await?;
                    cc.on_sent(bytes.len());                         // V4
                    rtx.record(p);                                   // V3
                    metrics::counter!(PACKETS_SENT, "kind" => "original").increment(1);
                    metrics::counter!(BYTES_SENT).increment(bytes.len() as u64);
                }
            }
            r = socket.recv_from(&mut rtcp_buf) => {
                let (n, _from) = r?;
                for pkt in RtcpPacket::parse_compound(&rtcp_buf[..n])? {   // V3
                    match pkt {
                        RtcpPacket::Nack(nack) => {
                            metrics::counter!(NACKS_TOTAL, "dir" => "received").increment(1);
                            for seq in nack.lost {
                                if let Some(p) = rtx.get(seq) {          // V3
                                    socket.send_to(&p.serialize(), remote).await?;
                                    metrics::counter!(RETRANSMITS).increment(1);
                                    metrics::counter!(PACKETS_SENT, "kind" => "retransmit")
                                        .increment(1);
                                }
                            }
                        }
                        RtcpPacket::ReceiverReport(rr) => {
                            cc.on_receiver_report(rr.fraction_lost, rr.jitter);   // V4
                            metrics::gauge!(TARGET_BITRATE).set(cc.target_bitrate() as f64);
                        }
                        RtcpPacket::Bye(_) => info!("peer sent RTCP BYE"),
                    }
                }
            }
            _ = shutdown.changed() => {
                if *shutdown.borrow() {
                    info!("sender shutting down");
                    break;
                }
            }
        }
    }
    Ok(())
}

/// Run the **receiver** side until shutdown (wired; V1/V2/V3 plug in).
pub async fn run_receiver(
    socket: Arc<UdpSocket>,
    cfg: Arc<TransportConfig>,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let self_ssrc: u32 = rand::random();
    let mut jitter = JitterBuffer::new(cfg.target_playout, H264_CLOCK_RATE, JITTER_CAPACITY);
    let mut buf = vec![0u8; 2048];
    let mut playout = interval(Duration::from_millis(PLAYOUT_TICK_MS));
    let mut feedback = interval(Duration::from_millis(FEEDBACK_INTERVAL_MS));
    let mut media_ssrc: u32 = 0;
    let mut peer: Option<SocketAddr> = None;

    info!(
        self_ssrc,
        "receiver started (idles until the first RTP datagram)"
    );

    loop {
        tokio::select! {
            r = socket.recv_from(&mut buf) => {
                let (n, from) = r?;
                peer = Some(from);
                metrics::counter!(PACKETS_RECEIVED).increment(1);
                metrics::counter!(BYTES_RECEIVED).increment(n as u64);
                let packet = RtpPacket::parse(&buf[..n])?;           // V1 (first todo a receiver hits)
                media_ssrc = packet.header.ssrc;
                jitter.insert(packet, Instant::now())?;              // V2
                metrics::gauge!(JITTER_BUFFER_DEPTH)
                    .set(jitter.stats().buffered_packets as f64);
            }
            _ = playout.tick() => {
                // Stay idle (and panic-free) until real traffic populates the buffer.
                if !jitter.is_empty() {
                    while let Some(frame) = jitter.pop_frame(Instant::now()) {   // V2
                        let _au = depacketize(&frame)?;              // V1
                        // playout sink: a decoder / eye consumes `_au` here.
                    }
                }
            }
            _ = feedback.tick() => {
                if let (Some(dst), false) = (peer, jitter.is_empty()) {
                    let missing = jitter.missing();                  // V2
                    if !missing.is_empty() {
                        metrics::counter!(PACKETS_LOST).increment(missing.len() as u64);
                        let nack = Nack::from_missing(self_ssrc, media_ssrc, &missing);
                        let datagram = RtcpPacket::Nack(nack).serialize();   // V3
                        socket.send_to(&datagram, dst).await?;
                        metrics::counter!(NACKS_TOTAL, "dir" => "sent").increment(1);
                    }
                    // A periodic RTCP Receiver Report (V3) would also be emitted here.
                }
            }
            _ = shutdown.changed() => {
                if *shutdown.borrow() {
                    info!("receiver shutting down");
                    break;
                }
            }
        }
    }
    Ok(())
}
