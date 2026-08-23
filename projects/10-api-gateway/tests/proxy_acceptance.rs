//! V1 acceptance tests — the reverse-proxy forwarding core (`src/proxy.rs`).
//!
//! These are **black-box** tests: they stand up a real HTTP backend on a real
//! socket, hand `proxy::forward` a real request, and assert only on what is
//! *observable* — what the backend received, what the client got back, how many
//! TCP connections were opened, and how long things took. They never look at how
//! `forward` is written, so they can't hand you the implementation.
//!
//! Each test maps to one bullet of SPEC.md V1 "Done when ALL true"; the test name
//! says which. Run them with:
//!
//! ```bash
//! cargo test -p api-gateway --test proxy_acceptance
//! cargo test -p api-gateway --test proxy_acceptance -- --nocapture   # see the panics
//! ```
//!
//! Until `forward` exists every one of them fails with `not yet implemented` —
//! that's the worklist, in priority order.
//!
//! **Scope note:** this file exercises V1 *only*. It builds a [`Backend`] by hand
//! and calls `forward` directly, so it never touches `Router::match_request` (V2),
//! `Balancer::pick` (V3), or `CircuitBreaker::allow` (V4) — all still `todo!()`.
//! If you wire circuit-breaker accounting (`backend.circuit.record_success()`)
//! into `forward` before building V4, these tests will panic inside V4's `todo!()`
//! instead. Add that accounting when you get to V4.

use std::collections::HashSet;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::body::{Body, Bytes};
use axum::extract::{ConnectInfo, Request, State};
use axum::http::{header, HeaderMap, Method, StatusCode};
use axum::response::Response;
use axum::routing::get;
use axum::Router as AxumRouter;
use futures_util::StreamExt;

use api_gateway::balancer::Backend;
use api_gateway::error::AppError;
use api_gateway::{proxy, upstream_client, UpstreamClient};

/// Upstream connect timeout for the test client — short, so the 502 test is quick.
const CONNECT_TIMEOUT: Duration = Duration::from_millis(500);
/// The per-request deadline the 504 test expects `forward` to enforce. It has to
/// clear the slowest *legitimate* body in this suite (~900ms) while still tripping
/// well inside the 504 test's 5s safety net — a deadline that also has to cover a
/// slow-but-honest download is the tension the real value lives in.
const REQUEST_DEADLINE: Duration = Duration::from_secs(2);

/// The single place this suite calls into V1.
///
/// `forward`'s signature is going to change: SPEC.md V1 requires an enforced
/// per-request deadline (→ 504), and the scaffold's doc comment tells you the
/// deadline lives on `AppState`. When you thread it through, update **this one
/// function** — e.g. `proxy::forward(client, backend, req, REQUEST_DEADLINE)` —
/// and the whole suite keeps compiling.
async fn forward(
    client: &UpstreamClient,
    backend: &Backend,
    req: Request,
) -> Result<Response, AppError> {
    let _ = REQUEST_DEADLINE;
    proxy::forward(client, backend, req).await
}

// ---------------------------------------------------------------------------
// The test backend: a real server that remembers exactly what it was sent.
// ---------------------------------------------------------------------------

/// One request as the *backend* saw it — the ground truth for "what did the proxy
/// actually put on the wire?".
#[derive(Clone, Debug)]
struct Seen {
    method: Method,
    path: String,
    /// Raw query string, so percent-encoding damage shows up.
    query: Option<String>,
    headers: HeaderMap,
    body_len: usize,
    /// FNV-1a of the body, so a multi-MiB upload is verified without keeping a copy.
    body_hash: u64,
    /// The client-side socket address. A distinct peer address means a distinct
    /// TCP connection — that's how the keep-alive test counts connections.
    peer: SocketAddr,
    /// When the request *head* arrived, before the body was read. A proxy that
    /// buffers the request body delays this; a streaming one does not.
    head_at: Instant,
}

impl Seen {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(name).and_then(|v| v.to_str().ok())
    }
}

#[derive(Clone, Default)]
struct Recorder(Arc<Mutex<Vec<Seen>>>);

struct TestBackend {
    addr: SocketAddr,
    recorder: Recorder,
}

impl TestBackend {
    /// Bind an ephemeral port and serve until the test ends.
    async fn start() -> Self {
        let recorder = Recorder::default();
        let app = AxumRouter::new()
            .route("/slow-body", get(slow_body))
            .route("/never-responds", get(never_responds))
            .route("/hop-by-hop-response", get(hop_by_hop_response))
            .fallback(record_and_echo)
            .with_state(recorder.clone());

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            // `with_connect_info` is what gives each handler the peer address, and
            // therefore what lets us count TCP connections.
            let svc = app.into_make_service_with_connect_info::<SocketAddr>();
            let _ = axum::serve(listener, svc).await;
        });
        Self { addr, recorder }
    }

    /// A `Backend` pointing at this server — what V3 would have handed V1.
    fn backend(&self) -> Arc<Backend> {
        Backend::new(&self.addr.to_string())
    }

    fn requests(&self) -> Vec<Seen> {
        self.recorder.0.lock().unwrap().clone()
    }

    /// The single request this backend saw. Panics (with a useful message) if the
    /// proxy never reached it.
    fn only_request(&self) -> Seen {
        let seen = self.requests();
        assert_eq!(
            seen.len(),
            1,
            "expected the backend to receive exactly 1 request, it saw {}",
            seen.len()
        );
        seen.into_iter().next().unwrap()
    }

    /// How many distinct TCP connections the proxy opened to this backend.
    fn connections_opened(&self) -> usize {
        self.requests()
            .iter()
            .map(|s| s.peer)
            .collect::<HashSet<_>>()
            .len()
    }
}

/// Catch-all handler: record everything, then echo the body straight back so the
/// response direction is checkable too.
async fn record_and_echo(
    State(rec): State<Recorder>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    req: Request,
) -> Response {
    // Taken *before* the body is read: this is the instant the head landed.
    let head_at = Instant::now();
    let (parts, body) = req.into_parts();
    let bytes = axum::body::to_bytes(body, usize::MAX)
        .await
        .unwrap_or_default();

    let status = parts
        .headers
        .get("x-test-status")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| StatusCode::from_bytes(v.as_bytes()).ok())
        .unwrap_or(StatusCode::OK);

    rec.0.lock().unwrap().push(Seen {
        method: parts.method.clone(),
        path: parts.uri.path().to_string(),
        query: parts.uri.query().map(str::to_string),
        headers: parts.headers.clone(),
        body_len: bytes.len(),
        body_hash: fnv1a(&bytes),
        peer,
        head_at,
    });

    Response::builder()
        .status(status)
        .header("x-backend-note", "handled-by-backend")
        .header("content-type", "application/octet-stream")
        .body(Body::from(bytes))
        .unwrap()
}

/// A response whose body trickles out over time. A proxy that buffers the upstream
/// body cannot return its head before the last chunk lands; a streaming one can.
async fn slow_body() -> Response {
    const GAP: Duration = Duration::from_millis(300);
    let stream = futures_util::stream::iter(["first-", "second-", "third"]).then(|chunk| async {
        tokio::time::sleep(GAP).await;
        Ok::<Bytes, std::io::Error>(Bytes::from_static(chunk.as_bytes()))
    });
    Response::builder()
        .status(StatusCode::OK)
        .body(Body::from_stream(stream))
        .unwrap()
}

/// Accepts the connection, then never answers — the classic "slow upstream".
async fn never_responds() -> Response {
    tokio::time::sleep(Duration::from_secs(60)).await;
    Response::new(Body::empty())
}

/// A backend that (rudely) sets hop-by-hop headers on its *response*. None of them
/// belong to the gateway→client connection, so none may reach the client.
async fn hop_by_hop_response() -> Response {
    Response::builder()
        .status(StatusCode::OK)
        .header("connection", "keep-alive, x-secret-hop")
        .header("x-secret-hop", "must-not-leak")
        .header("keep-alive", "timeout=5")
        .header("proxy-authenticate", "Basic realm=\"upstream\"")
        .header("x-backend-note", "end-to-end-header")
        .body(Body::from("upstream body"))
        .unwrap()
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

fn fnv1a(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        hash ^= u64::from(*b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

/// An inbound request shaped the way axum hands one to the gateway: origin-form
/// target (`/path?query`, no scheme or authority) plus a `Host` header.
fn inbound(method: Method, target: &str) -> axum::http::request::Builder {
    Request::builder()
        .method(method)
        .uri(target)
        .header(header::HOST, "gateway.test")
}

/// Read a response header as a `&str`.
fn res_header<'a>(res: &'a Response, name: &str) -> Option<&'a str> {
    res.headers().get(name).and_then(|v| v.to_str().ok())
}

/// Drain a response body to bytes. Draining matters for more than assertions:
/// hyper only returns a connection to the pool once its body is fully read.
async fn body_bytes(res: Response) -> Bytes {
    axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .expect("reading the proxied response body")
}

fn client() -> UpstreamClient {
    upstream_client(CONNECT_TIMEOUT)
}

/// `forward` returned an error where a response was expected — say which.
fn expect_response(result: Result<Response, AppError>) -> Response {
    match result {
        Ok(res) => res,
        Err(AppError::GatewayTimeout) => panic!(
            "`forward` timed out against a *healthy* backend. Your per-request \
             deadline is tighter than a legitimate slow body: this suite's slowest \
             honest response takes ~900ms, and `REQUEST_DEADLINE` here is \
             {REQUEST_DEADLINE:?}. A deadline that kills real downloads is a bug, \
             not a safety net."
        ),
        Err(e) => panic!(
            "`forward` returned an error against a healthy backend: {e}\n\
             Hint: a `Transfer-Encoding`/`Connection` header copied from the inbound \
             request will make hyper refuse to send the upstream request — those are \
             hop-by-hop and must be stripped."
        ),
    }
}

// ---------------------------------------------------------------------------
// V1 · "Method, path, query, response status and headers are preserved end to end"
// ---------------------------------------------------------------------------

#[tokio::test]
async fn method_path_query_and_status_survive_the_hop() {
    let upstream = TestBackend::start().await;
    let backend = upstream.backend();

    let req = inbound(Method::POST, "/api/v2/orders/42?q=two%20words&flag&n=7")
        .header("x-test-status", "418")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"hello":"world"}"#))
        .unwrap();

    let res = expect_response(forward(&client(), &backend, req).await);

    assert_eq!(
        res.status(),
        StatusCode::IM_A_TEAPOT,
        "the upstream's status code must reach the client unchanged"
    );
    assert_eq!(
        res_header(&res, "x-backend-note"),
        Some("handled-by-backend"),
        "end-to-end response headers from the upstream must reach the client"
    );
    assert_eq!(
        &body_bytes(res).await[..],
        br#"{"hello":"world"}"#,
        "the body must round-trip unchanged (the backend echoes what it received)"
    );

    let seen = upstream.only_request();
    assert_eq!(seen.method, Method::POST, "the method must be preserved");
    assert_eq!(
        seen.path, "/api/v2/orders/42",
        "the path must be preserved verbatim"
    );
    assert_eq!(
        seen.query.as_deref(),
        Some("q=two%20words&flag&n=7"),
        "the query string must be preserved *raw* — no re-encoding, no dropped keys"
    );
    assert_eq!(
        seen.header("content-type"),
        Some("application/json"),
        "end-to-end request headers must reach the upstream"
    );
}

// ---------------------------------------------------------------------------
// V1 · "Hop-by-hop headers are stripped between hops" (request direction)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn hop_by_hop_request_headers_never_reach_the_backend() {
    let upstream = TestBackend::start().await;
    let backend = upstream.backend();

    let req = inbound(Method::GET, "/hop-check")
        // `x-hop-token` is hop-by-hop *because this connection says so* — RFC 7230
        // §6.1: anything listed in `Connection` dies at this hop too.
        .header("connection", "keep-alive, x-hop-token")
        .header("x-hop-token", "must-not-leak")
        .header("keep-alive", "timeout=5, max=1000")
        .header("te", "trailers")
        .header("trailer", "x-checksum")
        .header("transfer-encoding", "chunked")
        .header("upgrade", "websocket")
        .header("proxy-authorization", "Basic c2VjcmV0")
        .header("proxy-connection", "keep-alive")
        // ...and two end-to-end headers that must survive the trip.
        .header("x-request-id", "req-123")
        .header("authorization", "Bearer app-token")
        .body(Body::empty())
        .unwrap();

    let _ = expect_response(forward(&client(), &backend, req).await);
    let seen = upstream.only_request();

    for hop in [
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "proxy-authorization",
        "proxy-connection",
        "x-hop-token",
    ] {
        assert!(
            seen.header(hop).is_none(),
            "the backend received hop-by-hop header `{hop}: {}` — hop-by-hop headers \
             belong to a single connection and must not be forwarded (`x-hop-token` \
             counts: it was named in the inbound `Connection` header)",
            seen.header(hop).unwrap_or_default()
        );
    }

    assert_eq!(
        seen.header("x-request-id"),
        Some("req-123"),
        "end-to-end headers must NOT be stripped — only hop-by-hop ones"
    );
    assert_eq!(
        seen.header("authorization"),
        Some("Bearer app-token"),
        "end-to-end headers must NOT be stripped — only hop-by-hop ones"
    );
}

// ---------------------------------------------------------------------------
// V1 · "Hop-by-hop headers are stripped between hops" (response direction)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn hop_by_hop_response_headers_never_reach_the_client() {
    let upstream = TestBackend::start().await;
    let backend = upstream.backend();

    let req = inbound(Method::GET, "/hop-by-hop-response")
        .body(Body::empty())
        .unwrap();
    let res = expect_response(forward(&client(), &backend, req).await);

    for hop in [
        "keep-alive",
        "proxy-authenticate",
        "x-secret-hop",
        "connection",
    ] {
        assert!(
            res.headers().get(hop).is_none(),
            "the client received hop-by-hop response header `{hop}` — stripping runs \
             in *both* directions, and `x-secret-hop` was named in the upstream's \
             `Connection` header"
        );
    }
    assert_eq!(
        res_header(&res, "x-backend-note"),
        Some("end-to-end-header"),
        "end-to-end response headers must still pass through"
    );
    assert_eq!(&body_bytes(res).await[..], b"upstream body");
}

// ---------------------------------------------------------------------------
// V1 · "The proxy sets provenance headers"
// ---------------------------------------------------------------------------

#[tokio::test]
async fn proxy_stamps_its_own_provenance_headers() {
    let upstream = TestBackend::start().await;
    let backend = upstream.backend();

    let req = inbound(Method::GET, "/who-sent-me")
        .body(Body::empty())
        .unwrap();
    let _ = expect_response(forward(&client(), &backend, req).await);
    let seen = upstream.only_request();

    assert!(
        seen.header("x-forwarded-for").is_some(),
        "the backend must be told who the original client was — set `X-Forwarded-For`"
    );
    assert_eq!(
        seen.header("x-forwarded-proto"),
        Some("http"),
        "the backend can't see the client's scheme; the proxy must state it in \
         `X-Forwarded-Proto` (this hop is plain HTTP)"
    );
    assert!(
        seen.header("via").is_some_and(|v| !v.is_empty()),
        "a proxy must announce itself in `Via` (RFC 7230 §5.7.1) so a hop chain is \
         traceable"
    );

    // The inbound `Host: gateway.test` names the *gateway*, not the upstream. Send
    // it on unchanged and a backend doing host-based vhosting sees the wrong site.
    // If you deliberately pass the original Host through (some gateways do), keep it
    // recoverable in `X-Forwarded-Host` — either policy satisfies this assertion.
    let host = seen.header("host").unwrap_or_default();
    let xf_host = seen.header("x-forwarded-host");
    assert!(
        host == upstream.addr.to_string() || xf_host == Some("gateway.test"),
        "the backend saw `Host: {host}` and no `X-Forwarded-Host`. Either rewrite \
         `Host` to the upstream authority (`{}`) or preserve the original in \
         `X-Forwarded-Host` — pick one and document it.",
        upstream.addr
    );
}

#[tokio::test]
async fn client_supplied_x_forwarded_for_is_not_blindly_trusted() {
    let upstream = TestBackend::start().await;
    let backend = upstream.backend();

    let req = inbound(Method::GET, "/spoof-check")
        .header("x-forwarded-for", "10.0.0.1")
        .body(Body::empty())
        .unwrap();
    let _ = expect_response(forward(&client(), &backend, req).await);

    let seen = upstream.only_request();
    let xff = seen
        .header("x-forwarded-for")
        .expect("`X-Forwarded-For` must be present");
    assert_ne!(
        xff, "10.0.0.1",
        "the client's `X-Forwarded-For: 10.0.0.1` reached the backend untouched — \
         anything a client sends is attacker-controlled, so a backend that trusts \
         XFF for rate limiting or ACLs is now spoofable. Append this hop's view of \
         the peer (`10.0.0.1, <peer>`) or replace the header outright; document which."
    );
}

// ---------------------------------------------------------------------------
// V1 · "A request/response body is streamed — memory stays bounded"
// ---------------------------------------------------------------------------

#[tokio::test]
async fn large_body_round_trips_intact() {
    let upstream = TestBackend::start().await;
    let backend = upstream.backend();

    // 8 MiB of non-repeating bytes: a chunking bug that drops, duplicates or
    // reorders a chunk changes the hash.
    let payload: Vec<u8> = (0..8 * 1024 * 1024).map(|i| (i % 251) as u8).collect();
    let expected = fnv1a(&payload);

    let req = inbound(Method::POST, "/upload")
        .body(Body::from(payload.clone()))
        .unwrap();
    let res = expect_response(forward(&client(), &backend, req).await);
    let echoed = body_bytes(res).await;

    let seen = upstream.only_request();
    assert_eq!(
        seen.body_len,
        payload.len(),
        "the upstream received {} of {} bytes — the request body was truncated",
        seen.body_len,
        payload.len()
    );
    assert_eq!(
        seen.body_hash, expected,
        "the upstream received the right *number* of bytes but not the right bytes"
    );
    assert_eq!(
        fnv1a(&echoed),
        expected,
        "the response body came back corrupted"
    );
}

#[tokio::test]
async fn response_body_is_streamed_not_buffered() {
    let upstream = TestBackend::start().await;
    let backend = upstream.backend();

    // The backend emits 3 chunks 300ms apart, so the body completes at ~900ms.
    let req = inbound(Method::GET, "/slow-body")
        .body(Body::empty())
        .unwrap();

    let started = Instant::now();
    let res = expect_response(forward(&client(), &backend, req).await);
    let head_latency = started.elapsed();

    assert!(
        head_latency < Duration::from_millis(250),
        "`forward` took {head_latency:?} to return the response head from a backend \
         whose body takes ~900ms to finish. That means the whole upstream body was \
         collected before responding — which is exactly what makes a 1 GiB download \
         cost 1 GiB of RSS. Map the upstream body into the axum body instead of \
         awaiting it."
    );

    let body = body_bytes(res).await;
    assert_eq!(
        &body[..],
        b"first-second-third",
        "streaming must not lose or reorder chunks"
    );
    assert!(
        started.elapsed() >= Duration::from_millis(600),
        "the full body arrived faster than the backend could have produced it — \
         the test backend is not behaving as expected"
    );
}

#[tokio::test]
async fn request_body_is_streamed_not_buffered() {
    let upstream = TestBackend::start().await;
    let backend = upstream.backend();

    // A body whose first chunk is ready immediately and whose second arrives 600ms
    // later. A streaming proxy sends the *head* upstream at once; a buffering one
    // can't send anything until the last byte is in hand.
    let stream = futures_util::stream::iter([(0u64, "part-1"), (600, "part-2")]).then(
        |(delay_ms, chunk)| async move {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
            Ok::<Bytes, std::io::Error>(Bytes::from_static(chunk.as_bytes()))
        },
    );

    let req = inbound(Method::POST, "/slow-upload")
        .body(Body::from_stream(stream))
        .unwrap();

    let started = Instant::now();
    let res = expect_response(forward(&client(), &backend, req).await);
    let _ = body_bytes(res).await;

    let seen = upstream.only_request();
    let head_delay = seen.head_at.duration_since(started);
    assert!(
        head_delay < Duration::from_millis(400),
        "the upstream didn't see the request head until {head_delay:?} in, but the \
         body's first chunk was ready immediately. The inbound body was collected \
         before the upstream request was sent — that's the 2 GiB upload buffering \
         into RAM. Hand the inbound body to the upstream request as a body, don't \
         await it."
    );
    assert_eq!(
        seen.body_len, 12,
        "both chunks must arrive upstream (`part-1part-2`)"
    );
}

// ---------------------------------------------------------------------------
// V1 · "Upstream connections are pooled/reused"
// ---------------------------------------------------------------------------

#[tokio::test]
async fn a_burst_of_requests_reuses_pooled_connections() {
    const BURST: usize = 20;

    let upstream = TestBackend::start().await;
    let backend = upstream.backend();
    // One client for the whole burst — the pool lives in the client, so a `forward`
    // that builds its own client per request throws the pool away every time.
    let client = client();

    for i in 0..BURST {
        let req = inbound(Method::GET, &format!("/burst/{i}"))
            .body(Body::empty())
            .unwrap();
        let res = expect_response(forward(&client, &backend, req).await);
        // Draining matters: hyper only returns a connection to the pool once its
        // body has been read to the end. Leak a body and you leak the connection.
        let _ = body_bytes(res).await;
    }

    let opened = upstream.connections_opened();
    assert_eq!(
        upstream.requests().len(),
        BURST,
        "every request in the burst must reach the backend"
    );
    assert!(
        opened <= 2,
        "{BURST} sequential requests opened {opened} TCP connections. Keep-alive \
         reuse should make that ~1: a fresh connection per request pays a handshake \
         (and a TLS one upstream) on every call. Check that you reuse the pooled \
         client, don't send `Connection: close`, and fully consume each upstream body."
    );
}

// ---------------------------------------------------------------------------
// V1 · "An unreachable or slow upstream yields a clean 502/504, never a panic
//       and never a hung request"
// ---------------------------------------------------------------------------

#[tokio::test]
async fn unreachable_upstream_is_a_502() {
    // Bind then drop: the port is now reliably nobody's, so connect() is refused.
    let addr = {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        listener.local_addr().unwrap()
    };
    let backend = Backend::new(&addr.to_string());

    let req = inbound(Method::GET, "/anything")
        .body(Body::empty())
        .unwrap();
    let result = forward(&client(), &backend, req).await;

    match result {
        Err(AppError::BadGateway) => {}
        Err(other) => panic!(
            "a refused upstream connection must map to `AppError::BadGateway` (502), \
             got `{other}`"
        ),
        Ok(res) => panic!(
            "a refused upstream connection produced a {} response — a transport \
             failure must surface as a 502, not a success",
            res.status()
        ),
    }
}

#[tokio::test]
async fn slow_upstream_is_a_504_not_a_hang() {
    let upstream = TestBackend::start().await;
    let backend = upstream.backend();

    let req = inbound(Method::GET, "/never-responds")
        .body(Body::empty())
        .unwrap();

    // The outer timeout is the test's safety net, not the deadline under test: if
    // it fires, `forward` enforced no deadline of its own.
    let guard = Duration::from_secs(5);
    let Ok(result) = tokio::time::timeout(guard, forward(&client(), &backend, req)).await else {
        panic!(
            "`forward` hung for {guard:?} against a backend that accepts the \
             connection and never answers. V1 requires an enforced per-request \
             deadline: without one, a single slow upstream ties up a connection and \
             a task per request until the gateway falls over. Thread \
             `AppState::request_timeout` into `forward` (see `REQUEST_DEADLINE` at \
             the top of this file) and fail the request when it expires."
        );
    };

    match result {
        Err(AppError::GatewayTimeout) => {}
        Err(other) => panic!(
            "an upstream that never responds must map to `AppError::GatewayTimeout` \
             (504), got `{other}`"
        ),
        Ok(res) => panic!(
            "an upstream that never responds produced a {} response",
            res.status()
        ),
    }
}
