//! V2 (part 2) — the publisher session state machine.
//!
//! One accepted RTMP connection = one `Session`. It drives the connection from "just
//! handshook" through the AMF0 command dance (`connect` → `createStream` → `publish`)
//! to "streaming media", using the chunk reader (V1, `rtmp.rs`) for framing and the
//! AMF0 codec (V2, `amf.rs`) for the commands. Once publishing, every audio/video
//! message is fed to the live fMP4 packager (V3, `fmp4.rs`) and its parts are pushed
//! into the shared window (`live.rs`) for viewers to pull.
//!
//! The accept loop below is **wired**; the interesting parts — answering each command,
//! enforcing the state transitions, and the media path — are the `todo!()`s.

use std::sync::Arc;
use std::time::Duration;

use tokio::net::{TcpListener, TcpStream};
use tokio::sync::watch;
use tracing::{info, warn};

use crate::error::AppError;
use crate::live::{IngestConfig, LiveRegistry, LiveStream};
use crate::rtmp::{self, ChunkStreamReader, Message};

/// Where a publisher connection is in its lifecycle. Media is only accepted in
/// `Publishing` — that gate (plus the stream-key check on the `publish` command) is
/// what stops an open ingest from being hijacked.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    /// Handshake done; waiting for `connect`.
    Connected,
    /// `connect` answered; waiting for `createStream`.
    AppConnected,
    /// `createStream` answered; waiting for `publish`.
    StreamCreated,
    /// `publish` accepted for an authorized key; media is flowing.
    Publishing,
}

/// One publisher connection.
pub struct Session {
    id: u64,
    stream: TcpStream,
    registry: Arc<LiveRegistry>,
    cfg: Arc<IngestConfig>,
    state: State,
    /// The stream key from `publish`, once authorized (never logged raw).
    stream_key: Option<String>,
    /// The shared live window this session publishes into, once publishing.
    live: Option<Arc<LiveStream>>,
}

impl Session {
    fn new(id: u64, stream: TcpStream, registry: Arc<LiveRegistry>) -> Self {
        let cfg = Arc::new(registry.config().clone());
        Self {
            id,
            stream,
            registry,
            cfg,
            state: State::Connected,
            stream_key: None,
            live: None,
        }
    }

    /// Run one publisher connection to completion: handshake, then a message loop.
    async fn run(mut self) {
        if let Err(e) = rtmp::handshake(&mut self.stream).await {
            warn!(session = self.id, error = %e, "handshake failed");
            return;
        }
        info!(session = self.id, "handshake complete");

        // Bound the largest message we'll assemble, so a malicious length can't OOM us.
        let max_msg = 16 * 1024 * 1024;
        let mut reader = ChunkStreamReader::new(max_msg);

        loop {
            match reader.read_message(&mut self.stream).await {
                Ok(msg) => {
                    if let Err(e) = self.handle(&mut reader, msg).await {
                        warn!(session = self.id, error = %e, "session error, closing");
                        break;
                    }
                }
                Err(e) => {
                    info!(session = self.id, error = %e, "connection ended");
                    break;
                }
            }
        }

        // Publisher gone: close out the live stream so its playlist gets ENDLIST.
        if let (Some(key), Some(live)) = (&self.stream_key, &self.live) {
            live.mark_ended();
            self.registry.close(key);
        }
    }

    /// Dispatch one reassembled RTMP message (V2/V3).
    ///
    /// TODO(V2/V3): route by `msg.type_id`:
    ///   - `AMF0_COMMAND` (20): decode with `amf::decode`; on the command name run the
    ///     state machine — `connect` ⇒ reply `_result`, advance to `AppConnected`;
    ///     `createStream` ⇒ reply `_result` with a stream id, advance to
    ///     `StreamCreated`; `publish` ⇒ check `registry.authorize(key)` (reject +
    ///     close if unknown), reply `onStatus` `NetStream.Publish.Start`, `open` the
    ///     live stream, advance to `Publishing`.
    ///   - `SET_CHUNK_SIZE` (1): already absorbed by the reader — nothing to do here.
    ///   - `AUDIO`/`VIDEO` (8/9): only valid in `Publishing` — reject otherwise. Parse
    ///     the FLV tag: the AVC/AAC **sequence headers** build the codec config → the
    ///     init segment (V3); subsequent tags become `fmp4::Sample`s fed to the
    ///     fragmenter, whose cut parts are pushed to `self.live` (V3/V4).
    ///   - window-ack / set-peer-bandwidth / user-control: handle or ignore per spec.
    /// A command arriving in the wrong state must not corrupt the session (reject or
    /// ignore — document which).
    async fn handle(
        &mut self,
        reader: &mut ChunkStreamReader,
        msg: Message,
    ) -> Result<(), AppError> {
        // Everything a real dispatcher touches, threaded so the shape is explicit.
        let _ = (
            reader,
            &msg,
            self.state,
            &self.cfg,
            &self.registry,
            &mut self.stream_key,
            &mut self.live,
        );
        todo!("V2/V3: handle command/media messages + drive the publish state machine")
    }
}

/// Accept RTMP connections until shutdown, spawning a [`Session`] per connection.
/// This is **wired** — the per-connection `Session::run` is where V1/V2/V3 live.
pub async fn accept_loop(
    listener: TcpListener,
    registry: Arc<LiveRegistry>,
    mut shutdown: watch::Receiver<bool>,
) {
    let mut next_id: u64 = 0;
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                match accepted {
                    Ok((stream, peer)) => {
                        let id = next_id;
                        next_id += 1;
                        // Nagle off: RTMP is latency-sensitive, small control writes
                        // shouldn't wait to coalesce.
                        let _ = stream.set_nodelay(true);
                        info!(session = id, %peer, "rtmp connection accepted");
                        let session = Session::new(id, stream, registry.clone());
                        tokio::spawn(session.run());
                    }
                    Err(e) => {
                        warn!(error = %e, "rtmp accept failed");
                        tokio::time::sleep(Duration::from_millis(100)).await;
                    }
                }
            }
            _ = shutdown.changed() => {
                if *shutdown.borrow() {
                    info!("rtmp accept loop shutting down");
                    break;
                }
            }
        }
    }
}
