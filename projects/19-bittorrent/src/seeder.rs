//! V6 — The seeder: serving pieces fairly under load. The upload half.
//!
//! Once you have pieces (or a complete file), you serve them: accept inbound peer
//! connections, complete the handshake (V4), and answer `request` messages with `piece`
//! data read from the verified store (V5). The catch — and the whole reason a single
//! seed can survive a swarm — is that **you can't upload to everyone at once**. Upload
//! bandwidth is finite; fan out to all N peers and every one crawls, buffers pile up,
//! and throughput collapses.
//!
//! So you run the **choke algorithm**: keep a small fixed number of **upload slots**
//! (regular unchokes, re-evaluated every ~10 s), plus one **optimistic unchoke**
//! (a random choked peer, rotated every ~30 s) so newcomers get a foot in the door.
//! Everyone else stays choked and waits. Cap total connections, and keep per-peer
//! buffers bounded (stream blocks from disk — never hold the whole file per connection).
//! That bounded, deliberate scheduling is what defeats the flash crowd (the boss).

use std::net::SocketAddr;
use std::sync::Arc;

use tokio::net::TcpListener;
use tokio::sync::watch;

use crate::client::Client;
use crate::error::AppError;

/// Default regular upload slots (the seeder unchokes at most this many + 1 optimistic).
pub const DEFAULT_UPLOAD_SLOTS: usize = 4;

/// Accept inbound peers until shutdown. Wired: the accept/shutdown select loop is done;
/// what each connection *does* is V6.
///
/// Spawned from `main` only when `RUN_SEEDER=true`, so the bare scaffold never reaches
/// the `todo!()`. The first peer that connects trips [`serve_peer`].
pub async fn accept_loop(
    listener: TcpListener,
    client: Arc<Client>,
    mut shutdown: watch::Receiver<bool>,
) {
    // The client owns the per-torrent piece stores and config the sessions read.
    let _ = &client;
    loop {
        tokio::select! {
            accepted = listener.accept() => match accepted {
                Ok((stream, addr)) => {
                    tracing::info!(%addr, "inbound peer connected");
                    // TODO(V6): register the peer, then run its session under the choke
                    // algorithm — hand it to a per-peer task, don't block the accept loop.
                    let _ = stream;
                    tokio::spawn(async move {
                        if let Err(e) = serve_peer(addr).await {
                            tracing::warn!(%addr, error = %e, "peer session ended");
                        }
                    });
                }
                Err(e) => tracing::warn!(error = %e, "accept failed"),
            },
            _ = shutdown.changed() => {
                tracing::info!("seeder shutting down: no longer accepting peers");
                break;
            }
        }
    }
}

/// Serve one connected peer for the life of the connection.
///
/// TODO(V6): handshake + exchange bitfields (V4), then loop: on `interested`, decide
/// whether this peer holds an upload slot; if unchoked, answer each valid `request` by
/// reading the block from the store ([`crate::download::PieceStore::read_block`]) and
/// sending a `piece`. Refuse a request for a piece you don't have or an out-of-range /
/// oversized one — refuse it, don't panic. Keep the per-peer read/write buffers bounded.
pub async fn serve_peer(addr: SocketAddr) -> Result<(), AppError> {
    let _ = addr;
    todo!("V6: seed to a peer — handshake, then answer requests within an upload slot")
}

/// The choke decision: given the peers we could upload to, pick which hold the regular
/// slots this round (plus who gets the optimistic unchoke).
///
/// TODO(V6): return at most `slots` peers to unchoke (a pure seed can favor the fastest
/// downloaders, round-robin, or random — pick and justify one in `docs/19-design.md`),
/// and separately choose one random *choked* peer as the optimistic unchoke. This is
/// the function the swarm's fairness — and the boss fight — rides on.
pub fn select_unchoked(candidates: &[SocketAddr], slots: usize) -> Vec<SocketAddr> {
    let _ = (candidates, slots);
    todo!("V6: choke algorithm — choose <= slots regular unchokes + 1 optimistic")
}

#[cfg(test)]
mod tests {
    // TODO(V6): prove fair, bounded seeding.
    //   - connect N > slots leechers to the seeder and assert at most `slots` (+1
    //     optimistic) are unchoked at any instant — it never fans out to all N;
    //   - `select_unchoked` never returns more than `slots` peers;
    //   - a peer requesting a piece we don't have, or an out-of-range/oversized block,
    //     is refused (no panic, no bytes served);
    //   - end-to-end: a leecher completes a full, SHA-1-verified download served purely
    //     by this seeder;
    //   - the boss fight (bench/): ≥ 50 concurrent leechers, bounded unchokes, bounded
    //     memory, zero corrupt files. See SPEC.md.
}
