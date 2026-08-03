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

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use tokio::net::{TcpListener, TcpStream};
use tokio::sync::watch;
use tracing::{info, warn};

use crate::amf::{self, Amf0};
use crate::error::AppError;
use crate::live::{IngestConfig, LiveRegistry, LiveStream};
use crate::rtmp::{self, msg_stream, ChunkStreamReader, Message, MsgType};

const MAX_MESSAGE_SIZE: usize = 16 * 1024 * 1024;
/// Window Acknowledgement Size / Set Peer Bandwidth value sent on `connect`.
const WINDOW_ACK_SIZE: u32 = 2_500_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CommandName {
    Connect,
    CreateStream,
    Publish,
}

impl std::str::FromStr for CommandName {
    type Err = AppError;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "connect" => Ok(CommandName::Connect),
            "createStream" => Ok(CommandName::CreateStream),
            "publish" => Ok(CommandName::Publish),
            _ => Err(AppError::BadRequest(format!("unknown command: {s}"))),
        }
    }
}

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

impl State {
    const fn as_str(&self) -> &'static str {
        match self {
            State::Connected => "Connected",
            State::AppConnected => "AppConnected",
            State::StreamCreated => "StreamCreated",
            State::Publishing => "Publishing",
        }
    }
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

        let mut reader = ChunkStreamReader::new(MAX_MESSAGE_SIZE);

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

    fn can_transition(&self, command_name: CommandName) -> Result<(), AppError> {
        match command_name {
            CommandName::Connect => {
                if self.state != State::Connected {
                    return Err(AppError::BadRequest(format!(
                        "connect command not allowed in state: {}",
                        self.state.as_str()
                    )));
                }
                Ok(())
            }
            CommandName::CreateStream => {
                if self.state != State::AppConnected {
                    return Err(AppError::BadRequest(format!(
                        "createStream command not allowed in state: {}",
                        self.state.as_str()
                    )));
                }
                Ok(())
            }
            CommandName::Publish => {
                if self.state != State::StreamCreated {
                    return Err(AppError::BadRequest(format!(
                        "publish command not allowed in state: {}",
                        self.state.as_str()
                    )));
                }
                Ok(())
            }
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
    ///
    /// A command arriving in the wrong state must not corrupt the session (reject or
    /// ignore — document which).
    async fn handle(
        &mut self,
        reader: &mut ChunkStreamReader,
        msg: Message,
    ) -> Result<(), AppError> {
        match MsgType::try_from(msg.type_id) {
            Ok(MsgType::Amf0Command) => {
                let command = amf::decode(&msg.payload)?;
                if command.is_empty() {
                    return Err(AppError::BadRequest("empty AMF0 command".to_string()));
                }
                let command_name = match command[0].as_str() {
                    Some(s) => match s.parse::<CommandName>() {
                        Ok(name) => name,
                        // releaseStream / FCPublish / etc. — ignore, never fatal.
                        Err(_) => return Ok(()),
                    },
                    None => return Err(AppError::BadRequest("invalid AMF0 command".to_string())),
                };
                self.can_transition(command_name)?;

                match command_name {
                    CommandName::Connect => self.handle_connect_stream(&command).await,
                    CommandName::CreateStream => self.handle_create_stream(&command).await,
                    CommandName::Publish => self.handle_publish(&command, msg.stream_id).await,
                }
            }
            // Already applied inside the chunk reader; nothing left for the session.
            Ok(MsgType::SetChunkSize) => Ok(()),
            Ok(
                MsgType::WindowAckSize
                | MsgType::SetPeerBandwidth
                | MsgType::Ack
                | MsgType::UserControl
                | MsgType::Abort
                | MsgType::Amf0Data,
            ) => Ok(()),
            Ok(MsgType::Audio | MsgType::Video) => {
                if self.state != State::Publishing {
                    return Err(AppError::BadRequest(format!(
                        "media message before publish (state {})",
                        self.state.as_str()
                    )));
                }
                // V3: FLV → fMP4 samples / init segment.
                let _ = (reader, &msg, &self.cfg, &self.live);
                Ok(())
            }
            Err(_) => Ok(()),
        }
    }

    /// Authorize a `publish` command, open the live window, and advance to
    /// [`State::Publishing`].
    ///
    /// Expects AMF args `publish, txn, null, publishingName, …` — the stream key
    /// is `publishingName` (`command[3]`). Calls [`LiveRegistry::authorize`]; on
    /// failure closes the session without logging the raw key. On success opens
    /// the shared [`LiveStream`], replies `onStatus` `NetStream.Publish.Start`
    /// on the NetStream id (falling back to [`msg_stream::DEFAULT`] if the
    /// client sent NetConnection `0`), and gates media acceptance.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::BadRequest`] when the key is missing or unauthorized,
    /// or propagates I/O failures from writing `onStatus`.
    async fn handle_publish(
        &mut self,
        command: &[Amf0],
        message_stream_id: u32,
    ) -> Result<(), AppError> {
        // publish(null, publishingName, publishingType) — key is the name.
        let key = command
            .get(3)
            .and_then(Amf0::as_str)
            .ok_or_else(|| AppError::BadRequest("publish missing stream key".into()))?;

        if !self.registry.authorize(key) {
            // Never log the raw key — it's a credential.
            warn!(
                session = self.id,
                "publish rejected: unauthorized stream key"
            );
            return Err(AppError::BadRequest("publish unauthorized".into()));
        }

        let live = self.registry.open(key);
        self.stream_key = Some(key.to_string());
        self.live = Some(live);

        let mut info = BTreeMap::new();
        [
            ("level", "status"),
            ("code", "NetStream.Publish.Start"),
            ("description", "Publishing started."),
        ]
        .into_iter()
        .for_each(|(k, v)| {
            info.insert(k.to_string(), Amf0::String(v.into()));
        });

        // onStatus rides the NetStream id from createStream, not NetConnection.
        let stream_id = if message_stream_id == msg_stream::NETCONNECTION {
            msg_stream::DEFAULT
        } else {
            message_stream_id
        };
        self.write_amf0_command(
            rtmp::csid::COMMAND,
            stream_id,
            &[
                Amf0::String("onStatus".into()),
                Amf0::Number(0.0),
                Amf0::Null,
                Amf0::Object(info),
            ],
        )
        .await?;

        self.state = State::Publishing;
        info!(session = self.id, "publish accepted");
        Ok(())
    }

    /// Answer a `createStream` command and advance to [`State::StreamCreated`].
    ///
    /// Replies with `_result` on the NetConnection stream, echoing the client's
    /// transaction id and assigning [`msg_stream::DEFAULT`] as the new NetStream
    /// id. The publisher stamps that id on later `publish` / media messages;
    /// `onStatus` replies ride the same stream.
    ///
    /// # Errors
    ///
    /// Propagates I/O failures from writing the `_result` chunk(s).
    async fn handle_create_stream(&mut self, command: &[Amf0]) -> Result<(), AppError> {
        let txn = command.get(1).and_then(Amf0::as_f64).unwrap_or(0.0);
        let payload = [
            Amf0::String("_result".into()),
            Amf0::Number(txn),
            Amf0::Null,
            Amf0::Number(f64::from(msg_stream::DEFAULT)),
        ];
        self.write_amf0_command(
            rtmp::csid::COMMAND,
            rtmp::msg_stream::NETCONNECTION,
            &payload,
        )
        .await?;
        self.state = State::StreamCreated;
        Ok(())
    }

    /// Answer a `connect` command and advance to [`State::AppConnected`].
    ///
    /// Sends the protocol-control bookkeeping many clients expect first —
    /// Window Acknowledgement Size and Set Peer Bandwidth (both
    /// [`WINDOW_ACK_SIZE`]) — then a NetConnection `_result` echoing the
    /// client's transaction id with `NetConnection.Connect.Success` and
    /// [`amf::object_encoding::AMF0`].
    ///
    /// Without this `_result`, encoders wait and never proceed to
    /// `createStream` / `publish`.
    ///
    /// # Errors
    ///
    /// Propagates I/O failures from writing the control messages or `_result`.
    async fn handle_connect_stream(&mut self, command: &[Amf0]) -> Result<(), AppError> {
        let txn = command.get(1).and_then(Amf0::as_f64).unwrap_or(0.0);

        let payload = WINDOW_ACK_SIZE.to_be_bytes().to_vec().into();
        let window_ack = Message::new(MsgType::WindowAckSize, msg_stream::NETCONNECTION, payload);
        rtmp::write_message(&mut self.stream, rtmp::csid::PROTOCOL_CONTROL, &window_ack).await?;

        let mut peer_bw = Vec::with_capacity(5);
        peer_bw.extend_from_slice(&WINDOW_ACK_SIZE.to_be_bytes());
        peer_bw.push(2); // limit type: dynamic
        let peer_bw = Message::new(
            MsgType::SetPeerBandwidth,
            rtmp::msg_stream::NETCONNECTION,
            peer_bw.into(),
        );
        rtmp::write_message(&mut self.stream, rtmp::csid::PROTOCOL_CONTROL, &peer_bw).await?;

        let mut info = BTreeMap::new();
        [
            ("level", "status"),
            ("code", "NetConnection.Connect.Success"),
            ("description", "Connection succeeded."),
        ]
        .into_iter()
        .for_each(|(key, value)| {
            info.insert(key.to_string(), Amf0::String(value.to_string()));
        });
        info.insert(
            "objectEncoding".to_string(),
            Amf0::Number(amf::object_encoding::AMF0),
        );

        self.write_amf0_command(
            rtmp::csid::COMMAND,
            rtmp::msg_stream::NETCONNECTION,
            &[
                Amf0::String("_result".into()),
                Amf0::Number(txn),
                Amf0::Null,
                Amf0::Object(info),
            ],
        )
        .await?;

        self.state = State::AppConnected;
        Ok(())
    }

    /// Write one AMF0 command as RTMP chunk(s) on `self.stream`.
    ///
    /// Builds the AMF0 body, wraps it in a type-20 [`Message`], and hands it to
    /// [`rtmp::write_message`] (fmt-0 header via `MessageHeaderField::as_bytes`).
    async fn write_amf0_command(
        &mut self,
        csid: u8,
        message_stream_id: u32,
        values: &[Amf0],
    ) -> Result<(), AppError> {
        let msg = Message::new(
            MsgType::Amf0Command,
            message_stream_id,
            amf::encode(values).into(),
        );
        rtmp::write_message(&mut self.stream, csid, &msg).await
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

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::Bytes;

    // The state machine writes small control replies (`_result`/`onStatus`, window-ack,
    // set-peer-bandwidth) to the socket. We drive `handle` over a real loopback socket
    // and assert on the resulting *state* rather than parsing the replies back: the
    // replies are a few hundred bytes, so they buffer in the kernel and `write_all`
    // returns without a reader. The client end is kept alive so those writes don't RST.

    async fn tcp_pair() -> (TcpStream, TcpStream) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (server, client) = tokio::join!(async { listener.accept().await.unwrap().0 }, async {
            TcpStream::connect(addr).await.unwrap()
        },);
        (server, client)
    }

    fn registry(keys: &[&str]) -> Arc<LiveRegistry> {
        let cfg = Arc::new(IngestConfig {
            stream_keys: keys.iter().map(|s| s.to_string()).collect(),
            target_part_secs: 0.3,
            target_segment_secs: 4.0,
            window_segments: 8,
        });
        Arc::new(LiveRegistry::new(cfg))
    }

    fn str_val(v: &str) -> Amf0 {
        Amf0::String(v.into())
    }

    /// An AMF0 command message (type 20) built from `values` on message stream `sid`.
    fn command(values: &[Amf0], sid: u32) -> Message {
        Message::new(MsgType::Amf0Command, sid, amf::encode(values).into())
    }

    fn connect_cmd() -> Message {
        command(&[str_val("connect"), Amf0::Number(1.0), Amf0::Null], 0)
    }
    fn create_stream_cmd() -> Message {
        command(&[str_val("createStream"), Amf0::Number(2.0), Amf0::Null], 0)
    }
    fn publish_cmd(key: &str) -> Message {
        // publish(null, publishingName, publishingType) — key is arg index 3.
        command(
            &[
                str_val("publish"),
                Amf0::Number(3.0),
                Amf0::Null,
                str_val(key),
                str_val("live"),
            ],
            1,
        )
    }
    fn media(kind: MsgType) -> Message {
        Message::new(kind, 1, Bytes::from_static(&[0x17, 0x00, 0x00]))
    }

    /// A session bound to a fresh loopback socket. The returned `TcpStream` is the
    /// client end — hold it so the server's writes don't hit a closed peer.
    async fn session(keys: &[&str]) -> (Session, ChunkStreamReader, TcpStream) {
        let (server, client) = tcp_pair().await;
        let sess = Session::new(0, server, registry(keys));
        (sess, ChunkStreamReader::new(MAX_MESSAGE_SIZE), client)
    }

    async fn drive_to_publishing(sess: &mut Session, reader: &mut ChunkStreamReader, key: &str) {
        sess.handle(reader, connect_cmd()).await.unwrap();
        sess.handle(reader, create_stream_cmd()).await.unwrap();
        sess.handle(reader, publish_cmd(key)).await.unwrap();
        assert_eq!(sess.state, State::Publishing);
    }

    #[test]
    fn command_name_parsing() {
        // AppError isn't PartialEq, so unwrap and compare the CommandName itself.
        assert_eq!(
            "connect".parse::<CommandName>().unwrap(),
            CommandName::Connect
        );
        assert_eq!(
            "createStream".parse::<CommandName>().unwrap(),
            CommandName::CreateStream
        );
        assert_eq!(
            "publish".parse::<CommandName>().unwrap(),
            CommandName::Publish
        );
        assert!("releaseStream".parse::<CommandName>().is_err());
    }

    #[tokio::test]
    async fn connect_advances_to_app_connected() {
        let (mut sess, mut reader, _client) = session(&[]).await;
        assert_eq!(sess.state, State::Connected);
        sess.handle(&mut reader, connect_cmd()).await.unwrap();
        assert_eq!(sess.state, State::AppConnected);
    }

    #[tokio::test]
    async fn full_sequence_reaches_publishing() {
        let (mut sess, mut reader, _client) = session(&["testkey"]).await;
        sess.handle(&mut reader, connect_cmd()).await.unwrap();
        assert_eq!(sess.state, State::AppConnected);
        sess.handle(&mut reader, create_stream_cmd()).await.unwrap();
        assert_eq!(sess.state, State::StreamCreated);
        sess.handle(&mut reader, publish_cmd("testkey"))
            .await
            .unwrap();

        assert_eq!(sess.state, State::Publishing);
        assert_eq!(sess.stream_key.as_deref(), Some("testkey"));
        assert!(sess.live.is_some());
        // The stream is now registered for viewers to find.
        assert!(sess.registry.get("testkey").is_some());
    }

    #[tokio::test]
    async fn media_rejected_before_publish() {
        // Audio/video in any pre-Publishing state must error and not change state.
        for kind in [MsgType::Audio, MsgType::Video] {
            let (mut sess, mut reader, _client) = session(&[]).await;
            let err = sess.handle(&mut reader, media(kind)).await.unwrap_err();
            assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
            assert_eq!(sess.state, State::Connected);

            // Also rejected after connect but before publish.
            sess.handle(&mut reader, connect_cmd()).await.unwrap();
            let err = sess.handle(&mut reader, media(kind)).await.unwrap_err();
            assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
            assert_eq!(sess.state, State::AppConnected);
        }
    }

    #[tokio::test]
    async fn media_accepted_after_publish() {
        let (mut sess, mut reader, _client) = session(&["k"]).await;
        drive_to_publishing(&mut sess, &mut reader, "k").await;
        sess.handle(&mut reader, media(MsgType::Audio))
            .await
            .unwrap();
        sess.handle(&mut reader, media(MsgType::Video))
            .await
            .unwrap();
        assert_eq!(sess.state, State::Publishing);
    }

    #[tokio::test]
    async fn unauthorized_publish_key_is_refused() {
        let (mut sess, mut reader, _client) = session(&["goodkey"]).await;
        sess.handle(&mut reader, connect_cmd()).await.unwrap();
        sess.handle(&mut reader, create_stream_cmd()).await.unwrap();

        let err = sess
            .handle(&mut reader, publish_cmd("badkey"))
            .await
            .unwrap_err();
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
        // Rejected: state, key, and live window are all untouched, and nothing was
        // registered — an unknown key never reaches Publishing (the takeover guard).
        assert_eq!(sess.state, State::StreamCreated);
        assert!(sess.stream_key.is_none());
        assert!(sess.live.is_none());
        assert!(sess.registry.get("badkey").is_none());
    }

    #[tokio::test]
    async fn empty_allowlist_authorizes_any_key() {
        // STREAM_KEYS empty ⇒ dev mode: any key publishes.
        let (mut sess, mut reader, _client) = session(&[]).await;
        drive_to_publishing(&mut sess, &mut reader, "whatever").await;
        assert_eq!(sess.stream_key.as_deref(), Some("whatever"));
    }

    #[tokio::test]
    async fn out_of_order_command_is_rejected() {
        // createStream before connect is not allowed and must not advance state.
        let (mut sess, mut reader, _client) = session(&[]).await;
        let err = sess
            .handle(&mut reader, create_stream_cmd())
            .await
            .unwrap_err();
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
        assert_eq!(sess.state, State::Connected);

        // publish before createStream, likewise.
        sess.handle(&mut reader, connect_cmd()).await.unwrap();
        let err = sess
            .handle(&mut reader, publish_cmd("k"))
            .await
            .unwrap_err();
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
        assert_eq!(sess.state, State::AppConnected);
    }

    #[tokio::test]
    async fn duplicate_connect_is_rejected() {
        let (mut sess, mut reader, _client) = session(&[]).await;
        sess.handle(&mut reader, connect_cmd()).await.unwrap();
        let err = sess.handle(&mut reader, connect_cmd()).await.unwrap_err();
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
        assert_eq!(sess.state, State::AppConnected);
    }

    #[tokio::test]
    async fn unknown_command_is_ignored_not_fatal() {
        // releaseStream / FCPublish et al. — not modelled, must be a no-op, not an error.
        let (mut sess, mut reader, _client) = session(&[]).await;
        sess.handle(
            &mut reader,
            command(
                &[str_val("releaseStream"), Amf0::Number(2.0), Amf0::Null],
                0,
            ),
        )
        .await
        .unwrap();
        assert_eq!(sess.state, State::Connected);
        sess.handle(
            &mut reader,
            command(&[str_val("FCPublish"), Amf0::Number(3.0), Amf0::Null], 0),
        )
        .await
        .unwrap();
        assert_eq!(sess.state, State::Connected);
    }

    #[tokio::test]
    async fn empty_amf0_command_errors() {
        let (mut sess, mut reader, _client) = session(&[]).await;
        let msg = Message::new(MsgType::Amf0Command, 0, Bytes::new());
        let err = sess.handle(&mut reader, msg).await.unwrap_err();
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn control_and_unknown_message_types_are_noops() {
        let (mut sess, mut reader, _client) = session(&[]).await;
        let noops = [
            Message::new(MsgType::SetChunkSize, 0, Bytes::from_static(&[0, 0, 0, 64])),
            Message::new(MsgType::WindowAckSize, 0, Bytes::from_static(&[0, 0, 0, 0])),
            Message::new(MsgType::Amf0Data, 0, Bytes::from_static(&[0])),
            // An unknown type id (MsgType::try_from errors) is tolerated, not fatal.
            Message::new(99u8, 0, Bytes::from_static(&[0])),
        ];
        for msg in noops {
            sess.handle(&mut reader, msg).await.unwrap();
            assert_eq!(sess.state, State::Connected);
        }
    }

    // ---- the three reply helpers, exercised directly ---------------------------------
    //
    // `handle` runs `can_transition` and then dispatches to one of `handle_connect_stream`
    // / `handle_create_stream` / `handle_publish`. These tests call each helper straight,
    // bypassing the guard, to pin *its own* contract: the exact reply bytes it writes and
    // the state it lands in. Replies are read back off the client end with a second reader
    // — the small control writes buffer in the kernel, so the helper's `write_all`s return
    // before we read (same reason the module note keeps the client alive).

    /// The `code` string inside an `onStatus`/`_result` info object, if present.
    fn status_code(info: &Amf0) -> Option<&str> {
        match info {
            Amf0::Object(map) => map.get("code").and_then(Amf0::as_str),
            _ => None,
        }
    }

    #[tokio::test]
    async fn handle_connect_stream_writes_bookkeeping_then_result() {
        let (mut sess, _reader, mut client) = session(&[]).await;
        // txn = 7 so we can prove it's echoed, not hard-coded.
        sess.handle_connect_stream(&[str_val("connect"), Amf0::Number(7.0), Amf0::Null])
            .await
            .unwrap();
        assert_eq!(sess.state, State::AppConnected);

        let mut rdr = ChunkStreamReader::new(MAX_MESSAGE_SIZE);
        // Flow-control pair many encoders expect before the `_result`.
        let ack = rdr.read_message(&mut client).await.unwrap();
        assert_eq!(MsgType::try_from(ack.type_id), Ok(MsgType::WindowAckSize));
        let bw = rdr.read_message(&mut client).await.unwrap();
        assert_eq!(MsgType::try_from(bw.type_id), Ok(MsgType::SetPeerBandwidth));

        let result = rdr.read_message(&mut client).await.unwrap();
        assert_eq!(MsgType::try_from(result.type_id), Ok(MsgType::Amf0Command));
        let vals = amf::decode(&result.payload).unwrap();
        assert_eq!(vals[0].as_str(), Some("_result"));
        assert_eq!(vals[1].as_f64(), Some(7.0)); // client's txn echoed
        assert_eq!(status_code(&vals[3]), Some("NetConnection.Connect.Success"));
    }

    #[tokio::test]
    async fn handle_create_stream_results_with_default_stream_id() {
        let (mut sess, _reader, mut client) = session(&[]).await;
        sess.handle_create_stream(&[str_val("createStream"), Amf0::Number(4.0), Amf0::Null])
            .await
            .unwrap();
        assert_eq!(sess.state, State::StreamCreated);

        let mut rdr = ChunkStreamReader::new(MAX_MESSAGE_SIZE);
        let result = rdr.read_message(&mut client).await.unwrap();
        let vals = amf::decode(&result.payload).unwrap();
        assert_eq!(vals[0].as_str(), Some("_result"));
        assert_eq!(vals[1].as_f64(), Some(4.0)); // txn echoed
                                                 // The assigned NetStream id the publisher must stamp on `publish`/media.
        assert_eq!(vals[3].as_f64(), Some(f64::from(msg_stream::DEFAULT)));
    }

    #[tokio::test]
    async fn handle_publish_authorizes_opens_and_replies_on_stream() {
        let (mut sess, _reader, mut client) = session(&["k"]).await;
        // A publish arriving on NetStream id 1 (what createStream handed out).
        let cmd = [
            str_val("publish"),
            Amf0::Number(3.0),
            Amf0::Null,
            str_val("k"),
            str_val("live"),
        ];
        sess.handle_publish(&cmd, 1).await.unwrap();

        // Side effects: state advanced, key recorded, live window opened + registered.
        assert_eq!(sess.state, State::Publishing);
        assert_eq!(sess.stream_key.as_deref(), Some("k"));
        assert!(sess.live.is_some());
        assert!(sess.registry.get("k").is_some());

        let mut rdr = ChunkStreamReader::new(MAX_MESSAGE_SIZE);
        let status = rdr.read_message(&mut client).await.unwrap();
        assert_eq!(status.stream_id, 1); // onStatus rides the NetStream, not NetConnection
        let vals = amf::decode(&status.payload).unwrap();
        assert_eq!(vals[0].as_str(), Some("onStatus"));
        assert_eq!(status_code(&vals[3]), Some("NetStream.Publish.Start"));
    }

    #[tokio::test]
    async fn handle_publish_onstatus_falls_back_off_netconnection_id() {
        // A client that leaves the publish on NetConnection 0 still gets onStatus on
        // the default NetStream id, never 0.
        let (mut sess, _reader, mut client) = session(&["k"]).await;
        let cmd = [
            str_val("publish"),
            Amf0::Number(3.0),
            Amf0::Null,
            str_val("k"),
            str_val("live"),
        ];
        sess.handle_publish(&cmd, msg_stream::NETCONNECTION)
            .await
            .unwrap();

        let mut rdr = ChunkStreamReader::new(MAX_MESSAGE_SIZE);
        let status = rdr.read_message(&mut client).await.unwrap();
        assert_eq!(status.stream_id, msg_stream::DEFAULT);
    }

    #[tokio::test]
    async fn handle_publish_refuses_unknown_key_without_side_effects() {
        let (mut sess, _reader, _client) = session(&["goodkey"]).await;
        let cmd = [
            str_val("publish"),
            Amf0::Number(3.0),
            Amf0::Null,
            str_val("badkey"),
            str_val("live"),
        ];
        let err = sess.handle_publish(&cmd, 1).await.unwrap_err();
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
        // Nothing mutated: no state change, no key, no live window, nothing registered.
        assert!(sess.stream_key.is_none());
        assert!(sess.live.is_none());
        assert!(sess.registry.get("badkey").is_none());
    }

    #[tokio::test]
    async fn handle_publish_missing_key_errors_not_panics() {
        // publish with no publishingName arg (command too short) — a BadRequest, not a panic.
        let (mut sess, _reader, _client) = session(&[]).await;
        let cmd = [str_val("publish"), Amf0::Number(3.0), Amf0::Null];
        let err = sess.handle_publish(&cmd, 1).await.unwrap_err();
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
        assert!(sess.live.is_none());
    }
}
