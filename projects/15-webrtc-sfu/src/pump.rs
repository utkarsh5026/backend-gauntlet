//! The UDP media-plane pump — **wired**, the analog of project 14's session loop.
//!
//! One muxed UDP socket carries STUN, RTP and RTCP (WebRTC bundles them). The pump does the
//! only thing the media plane needs at the top level: `recv_from`, [`classify`] the datagram
//! (RFC 7983 first-byte demux), hand it to the matching [`Sfu`] dispatch method, and send back
//! whatever datagrams that produced. All the interesting decisions happen inside those
//! dispatch methods and the vertical primitives they call.
//!
//! Media-plane errors are **dropped, not fatal** — a malformed datagram from an open UDP port
//! costs one packet, never the loop. A `todo!()` panic in a vertical ends this task (your
//! worklist) but leaves the admin/signaling HTTP server running, exactly like project 14.

use std::sync::Arc;

use tokio::net::UdpSocket;
use tokio::sync::watch;
use tracing::{debug, info, warn};

use crate::sfu::{Outgoing, Sfu};
use crate::wire::{classify, PacketKind};

/// Run the media pump until shutdown.
pub async fn run(
    socket: Arc<UdpSocket>,
    sfu: Arc<Sfu>,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    // Max UDP datagram we accept; anything larger is a broken/hostile peer.
    let mut buf = vec![0u8; 2048];
    info!("media pump running (STUN/RTP/RTCP muxed on the media socket; idles until traffic)");

    loop {
        tokio::select! {
            r = socket.recv_from(&mut buf) => {
                let (n, from) = r?;
                let datagram = &buf[..n];
                let result = match classify(datagram) {
                    PacketKind::Stun => sfu.handle_stun(from, datagram),   // V1
                    PacketKind::Rtp  => sfu.handle_rtp(from, datagram),    // V2 + V3
                    PacketKind::Rtcp => sfu.handle_rtcp(from, datagram),   // V4 + V2
                    PacketKind::Unknown => Ok(Vec::new()),                 // DTLS/TURN/garbage: drop
                };
                match result {
                    Ok(outs) => {
                        for Outgoing { dst, data } in outs {
                            if let Err(e) = socket.send_to(&data, dst).await {
                                warn!(error = %e, %dst, "media send failed");
                            }
                        }
                    }
                    // A bad datagram is a bounded, non-fatal drop — never a crash.
                    Err(e) => debug!(error = %e, %from, "dropped datagram"),
                }
            }
            _ = shutdown.changed() => {
                if *shutdown.borrow() {
                    info!("media pump shutting down");
                    break;
                }
            }
        }
    }
    Ok(())
}
