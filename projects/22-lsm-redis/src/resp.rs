//! V1 — RESP: the wire real `redis-cli` speaks. `src/resp.rs`.
//!
//! Redis clients talk **RESP** (REdis Serialization Protocol) over a raw TCP byte
//! stream. There is no HTTP, no length-prefixed envelope around the whole request —
//! just typed values back-to-back, each self-describing by its first byte:
//!
//! ```text
//!   +OK\r\n                        simple string
//!   -ERR unknown command\r\n       error
//!   :42\r\n                        integer
//!   $5\r\nhello\r\n                bulk string (length-prefixed bytes)
//!   $-1\r\n                        null bulk string  (a GET miss → nil)
//!   *2\r\n$3\r\nGET\r\n$1\r\nk\r\n array (how clients send a command)
//! ```
//!
//! A client sends every command as an **array of bulk strings** (`*N … $len … `).
//! Your job in V1 is the codec: pull one complete command off a byte buffer that may
//! hold a fraction of a frame *or* several pipelined frames at once, and serialize a
//! [`Resp`] reply back. Two properties make this the hard, interesting part:
//!
//!   1. **Streaming / partial frames.** TCP hands you arbitrary chunks. `parse_command`
//!      must return `Ok(None)` (need more bytes) *without consuming* a partial frame,
//!      and only advance the buffer once a whole command is present.
//!   2. **Pipelining.** A client may fire many commands before reading any reply; the
//!      buffer can hold several. The connection loop drains them in a `while let`.
//!
//! *Concept to internalize:* framing a request/response protocol over a raw stream —
//! length-prefix vs delimiter, why "read a line" is not enough, and how pipelining
//! falls out for free once parse/serialize are buffer-oriented. (Redis also accepts
//! *inline* commands like `PING\r\n` typed by a human at a socket — a nice stretch.)

use bytes::{Bytes, BytesMut};

use crate::error::AppError;

/// A parsed client command: its arguments as raw bulk-string bytes, e.g.
/// `[b"SET", b"user:1", b"alice"]`. The first element is the command name.
pub type Command = Vec<Bytes>;

/// A RESP value, used for the *reply* side (what the server sends back). The request
/// side is always an array of bulk strings, so it's decoded straight into a [`Command`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Resp {
    /// `+OK\r\n` — a short, non-binary status line.
    Simple(String),
    /// `-ERR …\r\n` — an error line (payload has no CRLF of its own).
    Error(String),
    /// `:N\r\n` — a 64-bit integer (e.g. the count from `DEL`).
    Integer(i64),
    /// `$len\r\n<bytes>\r\n` — a binary-safe bulk string (a stored value).
    Bulk(Bytes),
    /// `$-1\r\n` — the null bulk string: a `GET` miss, redis-cli renders it `(nil)`.
    Nil,
    /// `*len\r\n<elements…>` — an array (e.g. an empty reply to `COMMAND`).
    Array(Vec<Resp>),
}

impl Resp {
    /// Serialize this value onto the connection's outbound buffer, RESP-framed.
    ///
    /// TODO(V1): append the byte encoding of `self` to `out` — the type marker, the
    /// length prefix where one applies, the payload, and each `\r\n`. Encoding into a
    /// caller-owned buffer (not returning a fresh `Vec`) is what lets the connection
    /// loop batch a whole pipeline of replies into one `write_all`.
    pub fn encode(&self, out: &mut BytesMut) {
        let _ = out;
        todo!("V1: serialize a RESP value (simple/error/integer/bulk/nil/array) into `out`")
    }
}

/// Try to parse **one** complete command off the front of `buf`.
///
/// Contract (get this right and pipelining + partial reads both work):
///   - a whole command present → `Ok(Some(cmd))` **and `buf` is advanced past it**;
///   - only a partial frame present → `Ok(None)` and **`buf` is left untouched**
///     (the connection loop reads more bytes and calls again);
///   - malformed framing (bad type byte, non-numeric length, length over the cap) →
///     `Err(AppError::Protocol(_))` so the server can reply `-ERR` and close.
///
/// `max_bulk_len` caps a single bulk string's declared length so a hostile
/// `$1000000000000` header can't make you pre-allocate the server to death.
///
/// TODO(V1): implement the array-of-bulk-strings decode (the form every client uses).
/// Stretch: also accept an *inline* command (a bare `PING\r\n` with no `*`/`$`).
pub fn parse_command(buf: &mut BytesMut, max_bulk_len: usize) -> Result<Option<Command>, AppError> {
    let _ = (buf, max_bulk_len);
    todo!("V1: decode one `*N $len … ` command from `buf`, advancing it only when complete")
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the codec.
    //   - round-trip: for a set of Resp values, `parse(encode(v))` recovers the command
    //     form / value (a proptest — `prop_resp_roundtrip`);
    //   - partial frame: feeding the bytes of `*1\r\n$4\r\nPING\r\n` one at a time yields
    //     `Ok(None)` until the last byte, then `Ok(Some([b"PING"]))`, and the buffer is
    //     left untouched on each `None`;
    //   - pipelining: two commands concatenated in one buffer parse as two successive
    //     `Ok(Some(_))` with the right split;
    //   - a bulk length over `max_bulk_len` is rejected as a protocol error, not OOM.
}
