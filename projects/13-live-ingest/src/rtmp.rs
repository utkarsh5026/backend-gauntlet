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

use std::collections::HashMap;

use bytes::Bytes;
use tokio::io::{AsyncRead, AsyncWrite};

use crate::error::AppError;

/// The default RTMP chunk size before a `Set Chunk Size` control message changes it.
pub const DEFAULT_CHUNK_SIZE: usize = 128;

/// The fixed size of each handshake block (C1/S1/C2/S2), in bytes.
pub const HANDSHAKE_SIZE: usize = 1536;

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

/// Perform the RTMP handshake as the **server** over `stream` (V1).
///
/// TODO(V1): implement the C0/C1 ↔ S0/S1/S2 ↔ C2 exchange:
///   1. Read C0 (1 version byte, expect `0x03`) and C1 (`HANDSHAKE_SIZE` bytes:
///      4-byte time, 4-byte zero, then 1528 random bytes).
///   2. Write S0 (`0x03`), S1 (our own time + zero + 1528 random bytes — use
///      workspace `rand`), and S2 (an **echo** of C1's random block with our read time).
///   3. Read C2 (`HANDSHAKE_SIZE` bytes — the client's echo of our S1). You may
///      validate it or accept it (the simple/complex handshake distinction — note
///      which you chose in `docs/13-design.md`).
/// Getting a length or an echo wrong makes a real broadcaster hang up before sending
/// any command — so a completed handshake *is* the proof it's byte-correct.
pub async fn handshake<S>(stream: &mut S) -> Result<(), AppError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let _ = stream;
    todo!("V1: RTMP server handshake (C0/C1 <-> S0/S1/S2 <-> C2)")
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
}

/// Reassembles the RTMP chunk stream into whole [`Message`]s.
///
/// Holds the per-chunk-stream inheritance state and the current (negotiable) chunk
/// size. `read_message` pulls chunks off the socket until one message is complete.
pub struct ChunkStreamReader {
    chunk_size: usize,
    contexts: HashMap<u32, ChunkStreamCtx>,
    /// Guard against a malicious `message_length` — reject anything larger (security).
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

    /// The current negotiated chunk size (changed by a `Set Chunk Size` message).
    pub fn chunk_size(&self) -> usize {
        self.chunk_size
    }

    /// Apply a `Set Chunk Size` control message — the reassembly boundary changes from
    /// here on. Bound it so a publisher can't set an absurd size.
    pub fn set_chunk_size(&mut self, size: usize) {
        self.chunk_size = size.clamp(1, self.max_message_size);
    }

    /// Read chunks off `stream` until one complete [`Message`] is assembled (V1).
    ///
    /// TODO(V1): the chunk-stream reader — the heart of the vertical:
    ///   1. Read the **basic header**: top 2 bits = `fmt`, low 6 bits = chunk-stream
    ///      id (values 0 ⇒ 1 extra byte, 1 ⇒ 2 extra bytes, ≥2 ⇒ id as-is).
    ///   2. Read the **message header** by `fmt`:
    ///        fmt 0 → 11 bytes: ts(24) + len(24) + type(8) + stream-id(32, LE);
    ///        fmt 1 → 7 bytes:  ts-delta(24) + len(24) + type(8)  (inherit stream id);
    ///        fmt 2 → 3 bytes:  ts-delta(24)                       (inherit len/type/sid);
    ///        fmt 3 → 0 bytes:  inherit everything (a continuation or repeat).
    ///      A ts / ts-delta of `0xFFFFFF` means read a 4-byte **extended timestamp**.
    ///   3. Read up to `min(chunk_size, remaining)` payload bytes into the chunk
    ///      stream's `partial`. When `partial.len() == message_length`, a message is
    ///      complete: return it, absorbing a `Set Chunk Size` message via
    ///      `set_chunk_size` before handing it up.
    ///   Update the per-chunk-stream `ChunkStreamCtx` so the next fmt-1/2/3 inherits
    ///   correctly. **Range-check `message_length` against `max_message_size`** before
    ///   allocating — a malicious length must error, not OOM.
    pub async fn read_message<S>(&mut self, stream: &mut S) -> Result<Message, AppError>
    where
        S: AsyncRead + AsyncWrite + Unpin,
    {
        // `contexts` (fmt inheritance) and `chunk_size`/`max_message_size` (the
        // reassembly boundary + the OOM guard) are the state a real reader threads;
        // referenced so their role is explicit before you implement.
        let _ = (
            &self.contexts,
            self.chunk_size,
            self.max_message_size,
            stream,
        );
        todo!("V1: reassemble chunks (fmt 0-3 + extended ts + set-chunk-size) into a Message")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the reader over captured/synthetic chunk bytes:
    //   - a message split across several chunks reassembles with the right length and
    //     payload (`reassembles_multichunk_message`);
    //   - fmt 1/2/3 chunks inherit timestamp-delta / length / type / stream-id from the
    //     prior chunk on the same chunk-stream id (`chunk_header_fmt_inheritance`);
    //   - an extended timestamp (0xFFFFFF sentinel) is read from the 4 extra bytes;
    //   - a `Set Chunk Size` mid-stream changes the reassembly boundary;
    //   - random / truncated / oversized-length bytes never panic or over-allocate
    //     (`malformed_chunks_never_panic`).
}
