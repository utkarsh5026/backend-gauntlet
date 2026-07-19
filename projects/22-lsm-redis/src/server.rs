//! The RESP server: accept TCP connections and turn commands into engine calls.
//!
//! This is the glue between the wire (V1, [`crate::resp`]) and the store
//! ([`crate::engine`]). The accept loop and the per-connection read/dispatch/reply loop
//! are **wired** (same shape as the accept loops elsewhere in the gauntlet); the two
//! things they lean on are the `todo!()`s:
//!   - [`resp::parse_command`] / [`Resp::encode`] — the V1 codec (nothing parses until
//!     you write it, so the first byte a client sends trips V1);
//!   - `engine.get` / `set` / `delete` — the LSM read/write paths (V2→V7).
//!
//! Because the codec is unwritten, on the bare scaffold a connection is *accepted* but
//! the first command panics its task at V1 (the task dies, the runtime lives, the
//! connection drops) — that panic is your first worklist item. `PING`/`AUTH`/`COMMAND`
//! are answered here without touching the engine, so they light up the moment V1 works;
//! `SET`/`GET`/`DEL` then reach the engine `todo!()`s in order.
//!
//! The command table below is deliberately small — extending it with more redis
//! commands (INCR, EXPIRE, MGET, TTL, type commands…) is exactly the kind of surface a
//! real redis-compatible server grows, and is left as ongoing work.

use std::sync::Arc;

use bytes::BytesMut;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::watch;

use crate::engine::Engine;
use crate::error::AppError;
use crate::resp::{self, Command, Resp};

/// Connection-time policy read from the environment in `main`.
pub struct ServerConfig {
    /// If set, a connection must `AUTH <password>` before any other command (redis
    /// `requirepass`). `None` means the server is open. Never logged.
    pub requirepass: Option<Arc<str>>,
    /// Hard cap on a single bulk string's declared length (redis `proto-max-bulk-len`),
    /// so a hostile length header can't make the parser pre-allocate the box to death.
    pub max_request_bytes: usize,
}

/// Accept RESP clients until shutdown, one task per connection. Wired: the accept /
/// shutdown `select!` is done; what a connection *does* is [`handle_conn`].
pub async fn serve(
    listener: TcpListener,
    engine: Arc<Engine>,
    config: Arc<ServerConfig>,
    mut shutdown: watch::Receiver<bool>,
) {
    loop {
        tokio::select! {
            accepted = listener.accept() => match accepted {
                Ok((stream, addr)) => {
                    // TODO(security/limits): bound concurrent connections (redis
                    // `maxclients`) with a Semaphore so a connection flood can't
                    // exhaust fds/memory. TODO(observability): bump a connected-clients
                    // gauge here and drop it when the task ends.
                    let engine = engine.clone();
                    let config = config.clone();
                    let shutdown = shutdown.clone();
                    tokio::spawn(async move {
                        if let Err(e) = handle_conn(stream, engine, config, shutdown).await {
                            tracing::debug!(%addr, error = %e, "connection closed");
                        }
                    });
                }
                Err(e) => tracing::warn!(error = %e, "accept failed"),
            },
            _ = shutdown.changed() => {
                tracing::info!("RESP server shutting down: no longer accepting connections");
                break;
            }
        }
    }
}

/// Serve one connection: fill a buffer from the socket, drain every complete command
/// out of it (pipelining), dispatch each, and flush the batched replies. Wired on top
/// of the V1 codec — the loop shape is here; `parse_command`/`encode` are yours.
async fn handle_conn(
    mut stream: TcpStream,
    engine: Arc<Engine>,
    config: Arc<ServerConfig>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<(), AppError> {
    let (mut rd, mut wr) = stream.split();
    let mut buf = BytesMut::with_capacity(4096);
    let mut out = BytesMut::new();

    // A connection starts authenticated iff no password is required.
    let mut authed = config.requirepass.is_none();

    loop {
        // Drain every command already fully buffered before touching the socket again —
        // this is what makes pipelining fast (many commands, one syscall each way).
        while let Some(cmd) = resp::parse_command(&mut buf, config.max_request_bytes)? {
            let reply = dispatch(&engine, &config, &mut authed, cmd).await;
            reply.encode(&mut out);
        }
        if !out.is_empty() {
            wr.write_all(&out).await?;
            out.clear();
        }

        tokio::select! {
            read = rd.read_buf(&mut buf) => {
                if read? == 0 {
                    break; // peer closed the connection
                }
            }
            _ = shutdown.changed() => break,
        }
    }
    Ok(())
}

/// Map one parsed command to a reply. The engine calls are `await`ed; an engine error
/// becomes a `-ERR …` line (via [`AppError::to_resp_error`]) rather than dropping the
/// connection.
async fn dispatch(engine: &Engine, config: &ServerConfig, authed: &mut bool, cmd: Command) -> Resp {
    if cmd.is_empty() {
        return Resp::Error("ERR empty command".into());
    }
    // Command names are ASCII; keys/values stay raw bytes and are never stringified.
    let name = String::from_utf8_lossy(&cmd[0]).to_ascii_uppercase();

    // Gate the whole surface behind AUTH when a password is configured.
    if !*authed && !matches!(name.as_str(), "AUTH" | "HELLO" | "QUIT") {
        return Resp::Error("NOAUTH Authentication required.".into());
    }

    match name.as_str() {
        "PING" => match cmd.len() {
            1 => Resp::Simple("PONG".into()),
            2 => Resp::Bulk(cmd[1].clone()),
            _ => arity_err("ping"),
        },
        "ECHO" => match cmd.len() {
            2 => Resp::Bulk(cmd[1].clone()),
            _ => arity_err("echo"),
        },
        "AUTH" => {
            // `AUTH <password>` or (redis 6+) `AUTH <user> <password>`.
            let supplied = match cmd.len() {
                2 => &cmd[1],
                3 => &cmd[2],
                _ => return arity_err("auth"),
            };
            match &config.requirepass {
                None => Resp::Error("ERR Client sent AUTH, but no password is set.".into()),
                Some(pw) if supplied.as_ref() == pw.as_bytes() => {
                    *authed = true;
                    Resp::Simple("OK".into())
                }
                Some(_) => Resp::Error(
                    "WRONGPASS invalid username-password pair or user is disabled.".into(),
                ),
            }
        }
        "SET" => {
            if cmd.len() != 3 {
                return arity_err("set");
            }
            match engine.set(cmd[1].clone(), cmd[2].clone()).await {
                Ok(()) => Resp::Simple("OK".into()),
                Err(e) => Resp::Error(e.to_resp_error()),
            }
        }
        "GET" => {
            if cmd.len() != 2 {
                return arity_err("get");
            }
            match engine.get(&cmd[1]).await {
                Ok(Some(v)) => Resp::Bulk(v),
                Ok(None) => Resp::Nil,
                Err(e) => Resp::Error(e.to_resp_error()),
            }
        }
        "DEL" => {
            if cmd.len() < 2 {
                return arity_err("del");
            }
            let mut removed = 0i64;
            for key in &cmd[1..] {
                match engine.delete(key).await {
                    Ok(true) => removed += 1,
                    Ok(false) => {}
                    Err(e) => return Resp::Error(e.to_resp_error()),
                }
            }
            Resp::Integer(removed)
        }
        "EXISTS" => {
            if cmd.len() < 2 {
                return arity_err("exists");
            }
            let mut present = 0i64;
            for key in &cmd[1..] {
                match engine.get(key).await {
                    Ok(Some(_)) => present += 1,
                    Ok(None) => {}
                    Err(e) => return Resp::Error(e.to_resp_error()),
                }
            }
            Resp::Integer(present)
        }
        // Partial count (active memtable only) until the read path reconciles levels —
        // enough to answer redis-cli on the scaffold; make it exact once V4/V6 land.
        "DBSIZE" => Resp::Integer(engine.stats().keys_memtable as i64),
        // redis-cli probes COMMAND / COMMAND DOCS on connect; an empty array is a
        // harmless "I have no introspection to offer" that keeps the CLI happy.
        "COMMAND" => Resp::Array(Vec::new()),
        "QUIT" => Resp::Simple("OK".into()),
        other => Resp::Error(format!("ERR unknown command '{other}'")),
    }
}

fn arity_err(cmd: &str) -> Resp {
    Resp::Error(format!("ERR wrong number of arguments for '{cmd}' command"))
}
