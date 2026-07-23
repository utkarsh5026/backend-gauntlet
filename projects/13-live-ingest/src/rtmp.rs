//! V1 — RTMP handshake + chunk-stream reader: parse the wire by hand.
//!
//! RTMP runs over raw TCP. A connection opens with a three-message **handshake**
//! (C0/C1 ↔ S0/S1/S2 ↔ C2, each side echoing the other's 1528 random bytes), after
//! which data flows as a **chunk stream**: every logical message is split into chunks
//! of at most a negotiated size (default 128 bytes), each prefixed by a **basic
//! header** (a 2-bit `fmt` + a chunk-stream id of 1/2/3 bytes) and — for fmt 0/1/2 — a
//! **message header** that is *delta-compressed* against the previous chunk on the same
//! chunk-stream id (fmt 3 repeats it entirely). This module does the socket I/O and
//! reassembles chunks back into whole [`Message`]s; the codecs/commands inside are V2/V3.

use std::cmp::Ordering;
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use bytes::Bytes;
use rand::RngCore;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

use crate::error::AppError;

/// The default RTMP chunk size before a `Set Chunk Size` control message changes it.
pub const DEFAULT_CHUNK_SIZE: usize = 128;

/// The fixed size of each handshake block (C1/S1/C2/S2), in bytes.
pub const HANDSHAKE_SIZE: usize = 1536;

/// RTMP version byte exchanged in C0/S0 (plain RTMP; RTMPE would be different).
pub const RTMP_VERSION: u8 = 0x03;

/// Start of the 1528-byte random echo inside a C1/S1/C2/S2 block
/// (`time(4) + zero-or-time2(4) + random…`).
const HANDSHAKE_RANDOM_OFFSET: usize = 8;

// The minimum chunk size is 1 byte.
const MIN_CHUNK_SIZE: usize = 1;

/// One fully reassembled RTMP message: a typed payload on a message stream.
#[derive(Debug, Clone)]
pub struct Message {
    /// RTMP message type id (e.g. 20 = AMF0 command, 8 = audio, 9 = video, 1 = set
    /// chunk size, 18 = AMF0 data/metadata).
    pub type_id: u8,
    /// The message stream id this belongs to (set by `createStream`).
    pub stream_id: u32,
    /// Absolute timestamp in milliseconds (reconstructed from deltas + extended ts).
    pub timestamp: u32,
    /// The reassembled message body (all chunks concatenated).
    pub payload: Bytes,
}

/// RTMP message type ids the ingest cares about.
pub mod msg_type {
    pub const SET_CHUNK_SIZE: u8 = 1;
    pub const ABORT: u8 = 2;
    pub const ACK: u8 = 3;
    pub const USER_CONTROL: u8 = 4;
    pub const WINDOW_ACK_SIZE: u8 = 5;
    pub const SET_PEER_BANDWIDTH: u8 = 6;
    pub const AUDIO: u8 = 8;
    pub const VIDEO: u8 = 9;
    pub const AMF0_DATA: u8 = 18;
    pub const AMF0_COMMAND: u8 = 20;
}

/// Perform the RTMP **simple** handshake as the **server** over `stream` (V1).
///
/// Exchange: C0/C1 → S0/S1/S2 → C2. Each 1536-byte block is
/// `time(4) + zero-or-time2(4) + random(1528)`. S1 is original (our random);
/// S2 echoes C1's random with our read time in the time2 field; C2 must echo
/// S1's random (we check from [`HANDSHAKE_RANDOM_OFFSET`] — time2 is the peer's read time).
pub async fn handshake<S>(stream: &mut S) -> Result<(), AppError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let mut c0 = [0u8; 1];
    stream.read_exact(&mut c0).await?;
    if c0[0] != RTMP_VERSION {
        return Err(AppError::BadRequest(format!(
            "unsupported RTMP version {:#04x}",
            c0[0]
        )));
    }

    let mut c1 = [0u8; HANDSHAKE_SIZE];
    stream.read_exact(&mut c1).await?;

    let now = (SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u32)
        .to_be_bytes();

    let (s0, s1, s2) = {
        let s0 = [RTMP_VERSION];

        // S1: our time + zeros + 1528 fresh random bytes (client will echo this in C2).
        let mut s1 = [0u8; HANDSHAKE_SIZE];
        s1[0..4].copy_from_slice(&now);
        rand::rng().fill_bytes(&mut s1[HANDSHAKE_RANDOM_OFFSET..]);

        // S2: echo C1's time + random; time2 = when we read C1.
        let mut s2 = [0u8; HANDSHAKE_SIZE];
        s2[0..4].copy_from_slice(&c1[0..4]);
        s2[4..HANDSHAKE_RANDOM_OFFSET].copy_from_slice(&now);
        s2[HANDSHAKE_RANDOM_OFFSET..].copy_from_slice(&c1[HANDSHAKE_RANDOM_OFFSET..]);

        (s0, s1, s2)
    };

    stream.write_all(&s0).await?;
    stream.write_all(&s1).await?;
    stream.write_all(&s2).await?;

    let mut c2 = [0u8; HANDSHAKE_SIZE];
    stream.read_exact(&mut c2).await?;
    // Simple handshake: only the random echo is the liveness proof.
    if c2[HANDSHAKE_RANDOM_OFFSET..] != s1[HANDSHAKE_RANDOM_OFFSET..] {
        return Err(AppError::BadRequest("C2 does not echo S1 random".into()));
    }

    Ok(())
}

/// Per-chunk-stream decode context: the last header seen on a chunk-stream id, so a
/// fmt-1/2/3 chunk can inherit the fields it omits.
#[derive(Debug, Clone, Default)]
struct ChunkStreamCtx {
    timestamp: u32,
    timestamp_delta: u32,
    message_length: usize,
    type_id: u8,
    message_stream_id: u32,
    /// Bytes of the in-progress message accumulated so far (across chunks).
    partial: Vec<u8>,
    /// True when the last fmt 0/1/2 on this csid used an extended timestamp — fmt 3
    /// must then also carry (and we must consume) the 4-byte extended field.
    has_extended_timestamp: bool,
}

impl ChunkStreamCtx {
    fn update(&mut self, header: &MessageHeaderField) {
        match header {
            MessageHeaderField::Full {
                timestamp,
                message_length,
                type_id,
                message_stream_id,
            } => {
                self.timestamp = *timestamp;
                self.timestamp_delta = 0;
                self.message_length = *message_length;
                self.type_id = *type_id;
                self.message_stream_id = *message_stream_id;
            }
            MessageHeaderField::SameStream {
                timestamp_delta,
                message_length,
                type_id,
            } => {
                self.timestamp_delta = *timestamp_delta;
                self.timestamp = self.timestamp.wrapping_add(*timestamp_delta);
                self.message_length = *message_length;
                self.type_id = *type_id;
            }
            MessageHeaderField::TimestampDelta { timestamp_delta } => {
                self.timestamp_delta = *timestamp_delta;
                self.timestamp = self.timestamp.wrapping_add(*timestamp_delta);
            }
            MessageHeaderField::Inherited => {}
        }
    }

    /// Take the reassembled payload out of `partial` and form a completed [`Message`].
    fn take_message(&mut self) -> Message {
        let payload = Bytes::from(std::mem::take(&mut self.partial));
        Message {
            type_id: self.type_id,
            stream_id: self.message_stream_id,
            timestamp: self.timestamp,
            payload,
        }
    }
}

/// Parsed message-header fields for one chunk (what this fmt actually carried).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MessageHeaderField {
    /// fmt 0 — 11 bytes: absolute timestamp, length, type id, message stream id.
    Full {
        timestamp: u32,
        message_length: usize,
        type_id: u8,
        message_stream_id: u32,
    },
    /// fmt 1 — 7 bytes: timestamp delta, length, type id (stream id inherited).
    SameStream {
        timestamp_delta: u32,
        message_length: usize,
        type_id: u8,
    },
    /// fmt 2 — 3 bytes: timestamp delta only.
    TimestampDelta { timestamp_delta: u32 },
    /// fmt 3 — 0 bytes: inherit the previous header on this csid entirely.
    Inherited,
}

impl MessageHeaderField {
    /// The 24-bit timestamp or delta, if this fmt carried one (for extended-ts checks).
    fn timestamp_field(&self) -> Option<u32> {
        match self {
            Self::Full { timestamp, .. } => Some(*timestamp),
            Self::SameStream {
                timestamp_delta, ..
            }
            | Self::TimestampDelta { timestamp_delta } => Some(*timestamp_delta),
            Self::Inherited => None,
        }
    }

    /// Replace the 24-bit timestamp/delta after reading an extended timestamp.
    fn set_timestamp_field(&mut self, value: u32) {
        match self {
            Self::Full { timestamp, .. } => *timestamp = value,
            Self::SameStream {
                timestamp_delta, ..
            }
            | Self::TimestampDelta { timestamp_delta } => *timestamp_delta = value,
            Self::Inherited => {}
        }
    }
}

/// 24-bit timestamp/delta sentinel ⇒ a 4-byte extended timestamp follows.
const EXTENDED_TS_SENTINEL: u32 = 0x00FF_FFFF;

/// Basic-header `fmt` (0..=3).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct MessageHeaderFormat(u8);

impl MessageHeaderFormat {
    const fn is_inherited(self) -> bool {
        self.0 == 3
    }
}

impl TryFrom<u8> for MessageHeaderFormat {
    type Error = AppError;

    fn try_from(fmt: u8) -> Result<Self, Self::Error> {
        if fmt < 4 {
            Ok(Self(fmt))
        } else {
            Err(AppError::BadRequest(format!("invalid chunk fmt {fmt}")))
        }
    }
}

/// Chunk-stream id — the header-compression *lane* a chunk belongs to (not the
/// RTMP message stream id). Values 2..=63 fit in the basic header's low 6 bits;
/// 64+ use the one-/two-byte escapes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct Csid(u32);

impl Csid {
    const fn new(id: u32) -> Self {
        Self(id)
    }
}

/// Reassembles the RTMP chunk stream into whole [`Message`]s.
///
/// Holds the per-chunk-stream inheritance state and the current (negotiable) chunk
/// size. `read_message` pulls chunks off the socket until one message is complete.
pub struct ChunkStreamReader {
    chunk_size: usize,
    contexts: HashMap<Csid, ChunkStreamCtx>,
    max_message_size: usize,
}

impl ChunkStreamReader {
    pub fn new(max_message_size: usize) -> Self {
        Self {
            chunk_size: DEFAULT_CHUNK_SIZE,
            contexts: HashMap::new(),
            max_message_size,
        }
    }

    /// Apply a `Set Chunk Size` control message — the reassembly boundary changes from
    /// here on. Bound it so a publisher can't set an absurd size.
    pub fn set_chunk_size(&mut self, size: usize) {
        self.chunk_size = size.clamp(MIN_CHUNK_SIZE, self.max_message_size);
    }

    /// Read chunks from `stream` until one complete [`Message`] is reassembled.
    ///
    /// Per iteration:
    /// 1. [`Self::read_basic_header`] — `fmt` + [`Csid`]
    /// 2. [`Self::read_message_header`] — fields carried by that fmt (or none for fmt 3)
    /// 3. Extended timestamp — if the 24-bit ts/delta is `0xFFFFFF`, or if fmt 3
    ///    follows a prior extended header on this csid, consume the extra 4 bytes
    /// 4. Update [`ChunkStreamCtx`] for this csid:
    ///    - fmt 3 + empty `partial` → new message, re-apply stored timestamp delta
    ///    - fmt 3 + non-empty `partial` → continuation of the in-flight message
    ///    - fmt 0/1/2 → apply the new header (errors if a message was still incomplete)
    /// 5. Read `min(chunk_size, remaining)` payload bytes into `partial`
    /// 6. [`Self::assemble_chunk`] — return when `partial` reaches `message_length`
    ///    (and apply mid-stream Set Chunk Size if that was the message)
    ///
    /// `message_length` is range-checked against `max_message_size` before any further
    /// payload is accumulated, so a hostile length cannot OOM the reader.
    pub async fn read_message<S>(&mut self, stream: &mut S) -> Result<Message, AppError>
    where
        S: AsyncRead + AsyncWrite + Unpin,
    {
        loop {
            let (format, csid) = Self::read_basic_header(stream).await?;
            let mut msg_header = Self::read_message_header(stream, format).await?;

            // fmt 0/1/2: 0xFFFFFF in the 24-bit field ⇒ next 4 bytes are the real value.
            let had_extended = {
                if msg_header.timestamp_field() == Some(EXTENDED_TS_SENTINEL) {
                    let extended = read_u32_be(stream).await?;
                    msg_header.set_timestamp_field(extended);
                    true
                } else {
                    false
                }
            };

            // fmt 3 also carries the extended field when the prior fmt 0/1/2 did.
            let fmt3_needs_extended = format.is_inherited()
                && self
                    .contexts
                    .get(&csid)
                    .is_some_and(|ctx| ctx.has_extended_timestamp);
            if fmt3_needs_extended {
                let _ = read_u32_be(stream).await?;
            }

            let to_read = {
                let state = self.contexts.entry(csid).or_default();
                let continuation = !state.partial.is_empty();

                if format.is_inherited() {
                    if !continuation {
                        // New message with identical headers — re-apply the delta.
                        state.timestamp = state.timestamp.wrapping_add(state.timestamp_delta);
                    }
                    // else: continuation — keep header fields, just append payload below.
                } else {
                    if continuation {
                        return Err(AppError::BadRequest(
                            "chunk header started a new message while one was incomplete".into(),
                        ));
                    }
                    state.update(&msg_header);
                    state.has_extended_timestamp = had_extended;
                }

                if state.message_length.cmp(&self.max_message_size) == Ordering::Greater {
                    return Err(AppError::BadRequest(format!(
                        "message length {} exceeds max {}",
                        state.message_length, self.max_message_size
                    )));
                }

                state
                    .message_length
                    .saturating_sub(state.partial.len())
                    .min(self.chunk_size)
            };

            let mut payload_chunk = vec![0u8; to_read];
            if to_read > 0 {
                stream.read_exact(&mut payload_chunk).await?;
            }

            if let Some(msg) = self.assemble_chunk(csid, &payload_chunk)? {
                return Ok(msg);
            }
        }
    }

    /// Read the RTMP basic header: `fmt` (top 2 bits) + chunk-stream id.
    ///
    /// The low 6 bits of the first byte encode the csid:
    /// - `2..=63` — id is that value (1-byte header)
    /// - `0` — one extra byte follows; real id is `byte + 64` (64..=319)
    /// - `1` — two extra bytes follow (little-endian); real id is
    ///   `(hi << 8 | lo) + 64` (64..=65599)
    ///
    /// Returns the parsed [`MessageHeaderFormat`] and [`Csid`]. The message header
    /// (if any) is **not** read here — call [`Self::read_message_header`] next.
    async fn read_basic_header<S>(stream: &mut S) -> Result<(MessageHeaderFormat, Csid), AppError>
    where
        S: AsyncRead + Unpin,
    {
        let mut basic_header = [0u8; 1];
        stream.read_exact(&mut basic_header).await?;

        let format = MessageHeaderFormat::try_from(basic_header[0] >> 6)?;
        let csid_field = basic_header[0] & 0b0011_1111;

        // Low 6 bits 0/1 are length escapes, not ids — real csid is (extra bytes) + 64
        // so those ids don't collide with the one-byte range 2..=63.
        let csid = match csid_field {
            0 => {
                let mut b = [0u8; 1];
                stream.read_exact(&mut b).await?;
                Csid::new(u32::from(b[0]) + 64)
            }
            1 => {
                let mut b = [0u8; 2];
                stream.read_exact(&mut b).await?;
                Csid::new((u32::from(b[1]) << 8 | u32::from(b[0])) + 64)
            }
            id => Csid::new(u32::from(id)),
        };

        Ok((format, csid))
    }

    /// Read the message header that follows the basic header, sized by `format`.
    ///
    /// Wire layout depends on fmt:
    /// - **0** (`Full`): 11 bytes — `ts(24 BE) | len(24 BE) | type(8) | stream-id(32 LE)`
    /// - **1** (`SameStream`): 7 bytes — `ts-delta(24 BE) | len(24 BE) | type(8)`
    /// - **2** (`TimestampDelta`): 3 bytes — `ts-delta(24 BE)`
    /// - **3** (`Inherited`): 0 bytes — fields come from the prior chunk on this csid
    ///
    /// Does **not** read an extended timestamp; the caller checks for the `0xFFFFFF`
    /// sentinel on the 24-bit ts/delta and consumes those 4 bytes separately.
    async fn read_message_header<S>(
        stream: &mut S,
        format: MessageHeaderFormat,
    ) -> Result<MessageHeaderField, AppError>
    where
        S: AsyncRead + Unpin,
    {
        let u24_be = |bytes: &[u8]| u32::from_be_bytes([0, bytes[0], bytes[1], bytes[2]]);
        let mut buf = [0u8; 11];

        let msg_header = match format.0 {
            0 => {
                stream.read_exact(&mut buf[0..11]).await?;
                // Wire: ts(24 BE) | len(24 BE) | type(8) | stream-id(32 LE).
                let timestamp = u24_be(&buf[0..3]);
                let message_length = u24_be(&buf[3..6]) as usize;
                let type_id = buf[6];

                let message_stream_id = u32::from_le_bytes([buf[7], buf[8], buf[9], buf[10]]);
                MessageHeaderField::Full {
                    timestamp,
                    message_length,
                    type_id,
                    message_stream_id,
                }
            }
            1 => {
                stream.read_exact(&mut buf[0..7]).await?;
                let timestamp_delta = u24_be(&buf[0..3]);
                let message_length = u24_be(&buf[3..6]) as usize;
                let type_id = buf[6];
                MessageHeaderField::SameStream {
                    timestamp_delta,
                    message_length,
                    type_id,
                }
            }
            2 => {
                stream.read_exact(&mut buf[0..3]).await?;
                let timestamp_delta = u24_be(&buf[0..3]);
                MessageHeaderField::TimestampDelta { timestamp_delta }
            }
            3 => MessageHeaderField::Inherited,
            _ => unreachable!("MessageHeaderFormat only admits 0..=3"),
        };

        Ok(msg_header)
    }

    /// Append one chunk's payload bytes into the csid's `partial` buffer.
    ///
    /// Returns:
    /// - `Ok(None)` if more chunks are still needed (`partial.len() < message_length`)
    /// - `Ok(Some(message))` when the declared length is reached — `partial` is taken
    ///   out as the message body
    /// - `Err` if the buffer overshoots `message_length` (protocol / length bug)
    ///
    /// When the completed message is `Set Chunk Size` (type id 1), its 4-byte BE
    /// payload is applied via [`Self::set_chunk_size`] **before** returning, so the
    /// new boundary is in effect for the next chunk read.
    fn assemble_chunk(&mut self, csid: Csid, data: &[u8]) -> Result<Option<Message>, AppError> {
        let (msg, new_chunk_size) = {
            let state = self.contexts.get_mut(&csid).ok_or_else(|| {
                AppError::BadRequest("chunk stream context missing after header".into())
            })?;

            state.partial.extend_from_slice(data);
            match state.partial.len().cmp(&state.message_length) {
                Ordering::Less => return Ok(None),
                Ordering::Greater => {
                    return Err(AppError::BadRequest(
                        "assembled payload longer than declared message length".into(),
                    ))
                }
                Ordering::Equal => {}
            }

            let msg = state.take_message();

            let new_chunk_size = if msg.type_id == msg_type::SET_CHUNK_SIZE {
                if msg.payload.len() < 4 {
                    return Err(AppError::BadRequest(
                        "Set Chunk Size payload shorter than 4 bytes".into(),
                    ));
                }
                let size = u32::from_be_bytes([
                    msg.payload[0],
                    msg.payload[1],
                    msg.payload[2],
                    msg.payload[3],
                ]) as usize;
                Some(size)
            } else {
                None
            };
            (msg, new_chunk_size)
        };

        if let Some(size) = new_chunk_size {
            self.set_chunk_size(size);
        }
        Ok(Some(msg))
    }
}

async fn read_u32_be<S>(stream: &mut S) -> Result<u32, AppError>
where
    S: AsyncRead + Unpin,
{
    let mut buf = [0u8; 4];
    stream.read_exact(&mut buf).await?;
    Ok(u32::from_be_bytes(buf))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{duplex, AsyncReadExt, AsyncWriteExt, DuplexStream};

    /// A C1 block with a recognizable, non-random payload so we can tell it apart from
    /// the server's fresh S1 random and assert S2 echoes it. `time`/`time2` are set to
    /// distinct sentinels so we can check S2 copies C1's time into its `time` field.
    fn make_c1() -> [u8; HANDSHAKE_SIZE] {
        let mut c1 = [0u8; HANDSHAKE_SIZE];
        c1[0..4].copy_from_slice(&0x0102_0304u32.to_be_bytes()); // time
                                                                 // time2 stays zero (a real client sends 0 here).
        for (i, b) in c1[HANDSHAKE_RANDOM_OFFSET..].iter_mut().enumerate() {
            *b = (i % 251) as u8;
        }
        c1
    }

    /// Play a well-behaved RTMP client against the server end of a duplex pipe: send
    /// C0/C1, read S0/S1/S2, echo a valid C2. Returns `(s1, s2)` for assertions.
    async fn well_behaved_client(
        client: &mut DuplexStream,
        c1: &[u8; HANDSHAKE_SIZE],
    ) -> ([u8; HANDSHAKE_SIZE], [u8; HANDSHAKE_SIZE]) {
        client.write_all(&[RTMP_VERSION]).await.unwrap(); // C0
        client.write_all(c1).await.unwrap(); // C1

        let mut s0 = [0u8; 1];
        client.read_exact(&mut s0).await.unwrap();
        assert_eq!(s0[0], RTMP_VERSION, "S0 must carry the RTMP version");

        let mut s1 = [0u8; HANDSHAKE_SIZE];
        let mut s2 = [0u8; HANDSHAKE_SIZE];
        client.read_exact(&mut s1).await.unwrap();
        client.read_exact(&mut s2).await.unwrap();

        // A valid C2 echoes S1 verbatim (its random region is what the server checks).
        client.write_all(&s1).await.unwrap();

        (s1, s2)
    }

    #[tokio::test]
    async fn handshake_completes_with_well_behaved_client() {
        let (mut server, mut client) = duplex(16 * 1024);
        let server = tokio::spawn(async move { handshake(&mut server).await });

        let c1 = make_c1();
        let (s1, s2) = well_behaved_client(&mut client, &c1).await;

        server.await.unwrap().expect("handshake should succeed");

        // S1 is our own fresh random, not a copy of the client's C1 payload.
        assert_ne!(
            s1[HANDSHAKE_RANDOM_OFFSET..],
            c1[HANDSHAKE_RANDOM_OFFSET..],
            "S1 must be the server's own random, not an echo of C1"
        );
        // S2 echoes C1: same time field and same random region.
        assert_eq!(s2[0..4], c1[0..4], "S2 time must echo C1 time");
        assert_eq!(
            s2[HANDSHAKE_RANDOM_OFFSET..],
            c1[HANDSHAKE_RANDOM_OFFSET..],
            "S2 must echo C1's random back to the client"
        );
    }

    #[tokio::test]
    async fn handshake_rejects_unsupported_version() {
        let (mut server, mut client) = duplex(16 * 1024);
        let server = tokio::spawn(async move { handshake(&mut server).await });

        // Send a bogus C0; the server should reject before even reading C1.
        client.write_all(&[0x06]).await.unwrap();

        let err = server
            .await
            .unwrap()
            .expect_err("bad version must be rejected");
        assert!(
            matches!(err, AppError::BadRequest(_)),
            "expected BadRequest, got {err:?}"
        );
    }

    #[tokio::test]
    async fn handshake_rejects_c2_not_echoing_s1() {
        let (mut server, mut client) = duplex(16 * 1024);
        let server = tokio::spawn(async move { handshake(&mut server).await });

        let c1 = make_c1();
        client.write_all(&[RTMP_VERSION]).await.unwrap();
        client.write_all(&c1).await.unwrap();

        let mut sink = [0u8; 1 + HANDSHAKE_SIZE + HANDSHAKE_SIZE];
        client.read_exact(&mut sink).await.unwrap(); // drain S0/S1/S2

        // A C2 that does NOT echo S1's random (all zeros in the random region).
        let bad_c2 = [0u8; HANDSHAKE_SIZE];
        client.write_all(&bad_c2).await.unwrap();

        let err = server
            .await
            .unwrap()
            .expect_err("a C2 that doesn't echo S1 must be rejected");
        assert!(
            matches!(err, AppError::BadRequest(_)),
            "expected BadRequest, got {err:?}"
        );
    }

    #[tokio::test]
    async fn handshake_errors_on_truncated_client() {
        // Client drops the connection after C0 — the server's read_exact for C1 must
        // surface an error, not hang or panic.
        let (mut server, client) = duplex(16 * 1024);
        let server = tokio::spawn(async move { handshake(&mut server).await });

        let mut client = client;
        client.write_all(&[RTMP_VERSION]).await.unwrap();
        drop(client); // EOF before C1 completes

        let err = server
            .await
            .unwrap()
            .expect_err("truncated handshake must error");
        // EOF from read_exact is an io::Error, mapped to AppError::Other.
        assert!(
            matches!(err, AppError::Other(_)),
            "expected Other(io), got {err:?}"
        );
    }

    /// Decode a basic header off an in-memory byte slice (tokio impls `AsyncRead` for
    /// `&[u8]`), returning `(fmt, csid, bytes_left_unconsumed)` so tests can also assert
    /// exactly how many bytes the header consumed.
    async fn decode_basic_header(bytes: &[u8]) -> Result<(u8, Csid, usize), AppError> {
        let mut cursor = bytes;
        let (fmt, csid) = ChunkStreamReader::read_basic_header(&mut cursor).await?;
        Ok((fmt.0, csid, cursor.len()))
    }

    #[tokio::test]
    async fn basic_header_one_byte_reads_fmt_and_small_csid() {
        // Low 6 bits are the csid directly (2..=63); top 2 bits are the fmt.
        // 0b01_000101 = fmt 1, csid 5. Trailing 0xFF must be left untouched.
        let (fmt, csid, left) = decode_basic_header(&[0b01_000101, 0xFF]).await.unwrap();
        assert_eq!((fmt, csid), (1, Csid::new(5)));
        assert_eq!(left, 1, "a small-csid header is exactly one byte");

        // fmt 0, csid 2 (the smallest legal one-byte id).
        let (fmt, csid, _) = decode_basic_header(&[0b00_000010]).await.unwrap();
        assert_eq!((fmt, csid), (0, Csid::new(2)));

        // fmt 2, csid 63 (the largest one-byte id before the escapes).
        let (fmt, csid, _) = decode_basic_header(&[0b10_111111]).await.unwrap();
        assert_eq!((fmt, csid), (2, Csid::new(63)));
    }

    #[tokio::test]
    async fn basic_header_two_byte_escape_reads_extra_byte() {
        // Low 6 bits = 0 ⇒ csid is the next byte + 64 (range 64..=319).
        // First byte 0b11_000000 = fmt 3, escape 0; next byte 10 ⇒ csid 74.
        let (fmt, csid, left) = decode_basic_header(&[0b11_000000, 10, 0xAB]).await.unwrap();
        assert_eq!((fmt, csid), (3, Csid::new(74)));
        assert_eq!(left, 1, "the two-byte escape consumes exactly two bytes");

        // Boundary: extra byte 0 ⇒ csid 64 (first id that needs the escape).
        let (_, csid, _) = decode_basic_header(&[0b00_000000, 0]).await.unwrap();
        assert_eq!(csid, Csid::new(64));
    }

    #[tokio::test]
    async fn basic_header_three_byte_escape_is_little_endian() {
        // Low 6 bits = 1 ⇒ csid is the next two bytes (little-endian) + 64.
        // First byte 0b11_000001 = fmt 3, escape 1; bytes [0x01, 0x02] ⇒
        // (0x02 << 8 | 0x01) + 64 = 513 + 64 = 577.
        let (fmt, csid, left) = decode_basic_header(&[0b11_000001, 0x01, 0x02, 0x99])
            .await
            .unwrap();
        assert_eq!((fmt, csid), (3, Csid::new(577)));
        assert_eq!(
            left, 1,
            "the three-byte escape consumes exactly three bytes"
        );

        // The two escape bytes are low-then-high: [0xFF, 0x00] ⇒ 255 + 64 = 319,
        // distinguishing little- from big-endian (big-endian would give 65344 + 64).
        let (_, csid, _) = decode_basic_header(&[0b00_000001, 0xFF, 0x00])
            .await
            .unwrap();
        assert_eq!(csid, Csid::new(319));
    }

    #[tokio::test]
    async fn basic_header_truncated_errors_never_panics() {
        // Empty input: the first read_exact hits EOF.
        let err = decode_basic_header(&[])
            .await
            .expect_err("empty must error");
        assert!(matches!(err, AppError::Other(_)), "got {err:?}");

        // Two-byte escape announced but the extra byte is missing.
        let err = decode_basic_header(&[0b00_000000])
            .await
            .expect_err("missing escape byte must error");
        assert!(matches!(err, AppError::Other(_)), "got {err:?}");

        // Three-byte escape announced but only one of the two extra bytes is present.
        let err = decode_basic_header(&[0b00_000001, 0x01])
            .await
            .expect_err("short three-byte escape must error");
        assert!(matches!(err, AppError::Other(_)), "got {err:?}");
    }

    /// Decode a message header of a given `fmt` off an in-memory slice, returning the
    /// parsed field plus how many bytes were left unconsumed (so tests can assert the
    /// fmt-specific header width: 11 / 7 / 3 / 0 bytes).
    async fn decode_message_header(
        fmt: u8,
        bytes: &[u8],
    ) -> Result<(MessageHeaderField, usize), AppError> {
        let mut cursor = bytes;
        let field =
            ChunkStreamReader::read_message_header(&mut cursor, MessageHeaderFormat(fmt)).await?;
        Ok((field, cursor.len()))
    }

    #[tokio::test]
    async fn message_header_fmt0_full() {
        // ts(24 BE) | len(24 BE) | type(8) | stream-id(32 LE), 11 bytes.
        // ts=0x010203, len=0x000100 (256), type=20, sid=1 (LE: 01 00 00 00).
        let bytes = [
            0x01, 0x02, 0x03, // timestamp
            0x00, 0x01, 0x00, // message length
            20,   // type id
            0x01, 0x00, 0x00, 0x00, // message stream id (little-endian)
            0xEE, // trailer that must be left untouched
        ];
        let (field, left) = decode_message_header(0, &bytes).await.unwrap();
        assert_eq!(
            field,
            MessageHeaderField::Full {
                timestamp: 0x0001_0203,
                message_length: 256,
                type_id: 20,
                message_stream_id: 1,
            }
        );
        assert_eq!(left, 1, "fmt 0 consumes exactly 11 bytes");
    }

    #[tokio::test]
    async fn message_header_fmt1_same_stream() {
        // ts-delta(24 BE) | len(24 BE) | type(8), 7 bytes; stream id is inherited.
        let bytes = [0x00, 0x00, 0x40, 0x00, 0x00, 0x08, 9, 0xEE];
        let (field, left) = decode_message_header(1, &bytes).await.unwrap();
        assert_eq!(
            field,
            MessageHeaderField::SameStream {
                timestamp_delta: 0x40,
                message_length: 8,
                type_id: 9,
            }
        );
        assert_eq!(left, 1, "fmt 1 consumes exactly 7 bytes");
    }

    #[tokio::test]
    async fn message_header_fmt2_timestamp_delta() {
        // ts-delta(24 BE) only, 3 bytes.
        let bytes = [0x00, 0x12, 0x34, 0xEE];
        let (field, left) = decode_message_header(2, &bytes).await.unwrap();
        assert_eq!(
            field,
            MessageHeaderField::TimestampDelta {
                timestamp_delta: 0x1234
            }
        );
        assert_eq!(left, 1, "fmt 2 consumes exactly 3 bytes");
    }

    #[tokio::test]
    async fn message_header_fmt3_inherited_reads_nothing() {
        // fmt 3 carries no message header — the whole slice must be left untouched.
        let bytes = [0xAA, 0xBB, 0xCC];
        let (field, left) = decode_message_header(3, &bytes).await.unwrap();
        assert_eq!(field, MessageHeaderField::Inherited);
        assert_eq!(left, bytes.len(), "fmt 3 consumes zero header bytes");
    }

    #[tokio::test]
    async fn message_header_extended_sentinel_is_carried_verbatim() {
        // A 0xFFFFFF timestamp field is not resolved here — read_message_header returns
        // the sentinel, and read_message reads the 4 extended bytes separately.
        let (field, _) = decode_message_header(2, &[0xFF, 0xFF, 0xFF]).await.unwrap();
        assert_eq!(field.timestamp_field(), Some(EXTENDED_TS_SENTINEL));

        // fmt 0 with the sentinel in its timestamp position, likewise.
        let bytes = [
            0xFF, 0xFF, 0xFF, // timestamp = sentinel
            0x00, 0x00, 0x05, // len 5
            8,    // type
            0x00, 0x00, 0x00, 0x00, // sid 0
        ];
        let (field, _) = decode_message_header(0, &bytes).await.unwrap();
        assert_eq!(field.timestamp_field(), Some(EXTENDED_TS_SENTINEL));
    }

    #[tokio::test]
    async fn message_header_truncated_errors_never_panics() {
        // fmt 0 needs 11 bytes; give it 10.
        let err = decode_message_header(0, &[0u8; 10])
            .await
            .expect_err("short fmt 0 must error");
        assert!(matches!(err, AppError::Other(_)), "got {err:?}");

        // fmt 1 needs 7 bytes; give it 6.
        let err = decode_message_header(1, &[0u8; 6])
            .await
            .expect_err("short fmt 1 must error");
        assert!(matches!(err, AppError::Other(_)), "got {err:?}");

        // fmt 2 needs 3 bytes; give it 2.
        let err = decode_message_header(2, &[0u8; 2])
            .await
            .expect_err("short fmt 2 must error");
        assert!(matches!(err, AppError::Other(_)), "got {err:?}");
    }

    // ---- read_message: full chunk-stream reassembly ----------------------------------
    //
    // `read_message` needs `AsyncRead + AsyncWrite + Unpin`. A `std::io::Cursor<Vec<u8>>`
    // satisfies both (tokio impls AsyncRead for any `AsRef<[u8]>` and AsyncWrite for
    // `Cursor<Vec<u8>>`), so we can feed a hand-built chunk stream with no socket. The
    // reader only ever reads; the write half is never exercised.

    /// A 24-bit big-endian field (RTMP timestamps and lengths are u24 BE on the wire).
    fn u24(v: u32) -> [u8; 3] {
        [(v >> 16) as u8, (v >> 8) as u8, v as u8]
    }

    /// One basic-header byte for a small csid (2..=63): `fmt` in the top 2 bits.
    fn basic(fmt: u8, csid: u8) -> u8 {
        (fmt << 6) | csid
    }

    /// A full fmt-0 chunk header: basic byte + 11-byte message header.
    fn fmt0_header(csid: u8, ts: u32, len: u32, type_id: u8, sid: u32) -> Vec<u8> {
        let mut h = vec![basic(0, csid)];
        h.extend_from_slice(&u24(ts));
        h.extend_from_slice(&u24(len));
        h.push(type_id);
        h.extend_from_slice(&sid.to_le_bytes()); // stream id is little-endian
        h
    }

    /// Drive `reader` over `bytes` and return the next reassembled message.
    async fn read_one(reader: &mut ChunkStreamReader, bytes: Vec<u8>) -> Result<Message, AppError> {
        let mut stream = std::io::Cursor::new(bytes);
        reader.read_message(&mut stream).await
    }

    #[tokio::test]
    async fn single_chunk_message_reassembles() {
        // A message whose length fits inside one chunk (< default 128) is returned whole.
        let mut reader = ChunkStreamReader::new(1 << 20);
        let mut bytes = fmt0_header(3, 1000, 5, msg_type::AMF0_COMMAND, 1);
        bytes.extend_from_slice(&[1, 2, 3, 4, 5]);

        let msg = read_one(&mut reader, bytes).await.unwrap();
        assert_eq!(msg.type_id, msg_type::AMF0_COMMAND);
        assert_eq!(msg.stream_id, 1);
        assert_eq!(msg.timestamp, 1000);
        assert_eq!(&msg.payload[..], &[1, 2, 3, 4, 5]);
    }

    #[tokio::test]
    async fn reassembles_multichunk_message() {
        // 200 bytes > the default 128-byte chunk size, so it arrives as a 128-byte fmt-0
        // chunk + a 72-byte fmt-3 continuation. No chunk boundary may leak into the body.
        let mut reader = ChunkStreamReader::new(1 << 20);
        let payload: Vec<u8> = (0..200).map(|i| i as u8).collect();

        let mut bytes = fmt0_header(3, 1000, 200, msg_type::AUDIO, 5);
        bytes.extend_from_slice(&payload[..128]);
        bytes.push(basic(3, 3)); // fmt 3 = continuation of the same message
        bytes.extend_from_slice(&payload[128..]);

        let msg = read_one(&mut reader, bytes).await.unwrap();
        assert_eq!(msg.payload.len(), 200);
        assert_eq!(&msg.payload[..], &payload[..]);
        assert_eq!(msg.timestamp, 1000);
        assert_eq!(msg.stream_id, 5);
        assert_eq!(msg.type_id, msg_type::AUDIO);
    }

    #[tokio::test]
    async fn chunk_header_fmt_inheritance() {
        // Four messages on one csid exercising every fmt's inheritance:
        //   fmt 0 — absolute ts 1000, len 2, type 8, sid 5   (establishes all fields)
        //   fmt 1 — delta 100 (ts 1100), len 2, type 8       (inherits sid 5)
        //   fmt 2 — delta 50  (ts 1150)                       (inherits len/type/sid)
        //   fmt 3 — new message, re-applies stored delta 50   (ts 1200, all inherited)
        let mut reader = ChunkStreamReader::new(1 << 20);

        let mut bytes = fmt0_header(3, 1000, 2, msg_type::AUDIO, 5);
        bytes.extend_from_slice(&[0xA1, 0xA2]);

        bytes.push(basic(1, 3));
        bytes.extend_from_slice(&u24(100)); // ts delta
        bytes.extend_from_slice(&u24(2)); // len
        bytes.push(msg_type::AUDIO);
        bytes.extend_from_slice(&[0xB1, 0xB2]);

        bytes.push(basic(2, 3));
        bytes.extend_from_slice(&u24(50)); // ts delta only
        bytes.extend_from_slice(&[0xC1, 0xC2]);

        bytes.push(basic(3, 3)); // inherit everything, re-apply delta 50
        bytes.extend_from_slice(&[0xD1, 0xD2]);

        let mut stream = std::io::Cursor::new(bytes);
        let mut got = Vec::new();
        for _ in 0..4 {
            got.push(reader.read_message(&mut stream).await.unwrap());
        }

        // Every message inherits stream id 5, type 8, length 2.
        for m in &got {
            assert_eq!(m.stream_id, 5);
            assert_eq!(m.type_id, msg_type::AUDIO);
            assert_eq!(m.payload.len(), 2);
        }
        let ts: Vec<u32> = got.iter().map(|m| m.timestamp).collect();
        assert_eq!(ts, vec![1000, 1100, 1150, 1200]);
        assert_eq!(&got[0].payload[..], &[0xA1, 0xA2]);
        assert_eq!(&got[3].payload[..], &[0xD1, 0xD2]);
    }

    #[tokio::test]
    async fn extended_timestamp_read_from_four_bytes() {
        // A 24-bit ts of 0xFFFFFF is a sentinel: the real value is the next 4 bytes.
        let mut reader = ChunkStreamReader::new(1 << 20);
        let mut bytes = vec![basic(0, 3)];
        bytes.extend_from_slice(&u24(EXTENDED_TS_SENTINEL));
        bytes.extend_from_slice(&u24(2)); // len
        bytes.push(msg_type::AUDIO);
        bytes.extend_from_slice(&1u32.to_le_bytes()); // sid
        bytes.extend_from_slice(&65_536u32.to_be_bytes()); // extended timestamp
        bytes.extend_from_slice(&[0x01, 0x02]);

        let msg = read_one(&mut reader, bytes).await.unwrap();
        assert_eq!(msg.timestamp, 65_536);
        assert_eq!(&msg.payload[..], &[0x01, 0x02]);
    }

    #[tokio::test]
    async fn extended_timestamp_carried_on_fmt3_continuation() {
        // When a fmt-0 chunk used an extended timestamp, its fmt-3 continuation on the
        // same csid must ALSO carry (and the reader must consume) the 4 extended bytes,
        // or the payload desyncs. 130-byte message across a 128 + 2 boundary.
        let mut reader = ChunkStreamReader::new(1 << 20);
        let payload: Vec<u8> = (0..130).map(|i| i as u8).collect();
        let ext = 131_072u32;

        let mut bytes = vec![basic(0, 3)];
        bytes.extend_from_slice(&u24(EXTENDED_TS_SENTINEL));
        bytes.extend_from_slice(&u24(130));
        bytes.push(msg_type::VIDEO);
        bytes.extend_from_slice(&1u32.to_le_bytes());
        bytes.extend_from_slice(&ext.to_be_bytes());
        bytes.extend_from_slice(&payload[..128]);

        bytes.push(basic(3, 3));
        bytes.extend_from_slice(&ext.to_be_bytes()); // fmt-3 repeats the extended field
        bytes.extend_from_slice(&payload[128..]);

        let msg = read_one(&mut reader, bytes).await.unwrap();
        assert_eq!(msg.timestamp, ext);
        assert_eq!(msg.payload.len(), 130);
        assert_eq!(&msg.payload[..], &payload[..]);
    }

    #[tokio::test]
    async fn set_chunk_size_changes_reassembly_boundary() {
        // A Set Chunk Size (type 1) message shrinks the boundary to 4; the following
        // 10-byte message is then only reassembled correctly across 4 + 4 + 2 chunks —
        // proof the new boundary took effect (at the default 128 it would misparse).
        let mut reader = ChunkStreamReader::new(1 << 20);

        let mut bytes = fmt0_header(2, 0, 4, msg_type::SET_CHUNK_SIZE, 0);
        bytes.extend_from_slice(&4u32.to_be_bytes()); // new chunk size = 4

        let data: Vec<u8> = (0x10..0x1A).collect(); // 10 distinct bytes
        bytes.extend(fmt0_header(3, 0, 10, msg_type::AUDIO, 1));
        bytes.extend_from_slice(&data[0..4]);
        bytes.push(basic(3, 3));
        bytes.extend_from_slice(&data[4..8]);
        bytes.push(basic(3, 3));
        bytes.extend_from_slice(&data[8..10]);

        let mut stream = std::io::Cursor::new(bytes);
        let scs = reader.read_message(&mut stream).await.unwrap();
        assert_eq!(scs.type_id, msg_type::SET_CHUNK_SIZE);

        let msg = reader.read_message(&mut stream).await.unwrap();
        assert_eq!(msg.type_id, msg_type::AUDIO);
        assert_eq!(&msg.payload[..], &data[..]);
    }

    #[tokio::test]
    async fn oversized_message_length_rejected() {
        // A declared length beyond max_message_size must error BEFORE any payload is
        // allocated — the OOM guard. No payload bytes are even supplied.
        let mut reader = ChunkStreamReader::new(100);
        let bytes = fmt0_header(3, 0, 1000, msg_type::AUDIO, 1); // len 1000 > max 100

        let err = read_one(&mut reader, bytes)
            .await
            .expect_err("oversized length must be rejected");
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn new_message_while_incomplete_rejected() {
        // A fmt-0 header (a brand-new message) arriving while the csid still has an
        // in-flight partial message is a protocol violation, not silent data loss.
        let mut reader = ChunkStreamReader::new(1 << 20);
        reader.set_chunk_size(4);

        let mut bytes = fmt0_header(3, 0, 10, msg_type::AUDIO, 1);
        bytes.extend_from_slice(&[0, 1, 2, 3]); // 4 of 10 bytes → still incomplete
        bytes.extend(fmt0_header(3, 0, 10, msg_type::AUDIO, 1)); // new message, too soon

        let err = read_one(&mut reader, bytes)
            .await
            .expect_err("a new message mid-reassembly must be rejected");
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn truncated_chunk_stream_errors() {
        // A header that promises payload the stream doesn't have surfaces an io error.
        let mut reader = ChunkStreamReader::new(1 << 20);
        let mut bytes = fmt0_header(3, 0, 10, msg_type::AUDIO, 1);
        bytes.extend_from_slice(&[0, 1, 2]); // only 3 of 10 payload bytes, then EOF

        let err = read_one(&mut reader, bytes)
            .await
            .expect_err("truncated payload must error");
        assert!(matches!(err, AppError::Other(_)), "got {err:?}");
    }

    #[test]
    fn assemble_chunk_rejects_payload_over_declared_length() {
        // `read_message` never feeds more than `remaining` bytes, so the overshoot guard
        // in `assemble_chunk` is unreachable from the wire — exercise it directly by
        // seeding a context that expects 4 bytes and handing it 5.
        let mut reader = ChunkStreamReader::new(1 << 20);
        let csid = Csid::new(3);
        reader.contexts.insert(
            csid,
            ChunkStreamCtx {
                message_length: 4,
                type_id: msg_type::AUDIO,
                ..Default::default()
            },
        );

        let err = reader
            .assemble_chunk(csid, &[1, 2, 3, 4, 5])
            .expect_err("payload longer than declared length must be rejected");
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
    }

    proptest::proptest! {
        /// The reader must never panic, over-allocate, or hang on adversarial bytes —
        /// any random buffer yields a clean `Ok`/`Err`, never a crash. The `Cursor`
        /// hits EOF once drained, so a malformed stream always terminates.
        #[test]
        fn malformed_chunks_never_panic(bytes in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..512)) {
            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async {
                let mut reader = ChunkStreamReader::new(1 << 20);
                let mut stream = std::io::Cursor::new(bytes);
                // Loop so multi-message garbage is fully consumed; bounded by EOF.
                while reader.read_message(&mut stream).await.is_ok() {}
            });
        }
    }
}
