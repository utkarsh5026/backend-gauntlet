//! mTLS scaffolding (security horizontal).
//!
//! Building these rustls configs is the work; wrapping a finished config in an
//! acceptor is one line (given). The gateway terminates TLS from clients and can,
//! for a mutually-authenticated data path, present a client cert to upstreams. All
//! trust roots / cert paths come from config — never hard-coded. See SPEC.md
//! (Security → mTLS). Wire these into `main` once built (see the `TODO(mTLS)` there).

use std::sync::Arc;

/// Build the rustls `ServerConfig` used to terminate TLS from clients. If
/// `client_ca` is set, require + verify client certificates (mTLS at the edge) so
/// only holders of a cert signed by that CA may connect.
///
/// TODO(mTLS): load the cert chain + private key (`rustls-pemfile`), and install a
/// client-cert verifier when `client_ca` is provided.
pub fn server_config(
    cert_path: &str,
    key_path: &str,
    client_ca: Option<&str>,
) -> anyhow::Result<Arc<rustls::ServerConfig>> {
    let _ = (cert_path, key_path, client_ca);
    todo!("mTLS: build a rustls ServerConfig, optionally requiring client certs")
}

/// Build the rustls `ClientConfig` the gateway uses when talking to upstreams over
/// TLS — verifying the upstream's cert against `ca_path`, and (for mTLS to
/// upstreams) presenting the gateway's own `client_cert` = `(cert_path, key_path)`.
///
/// TODO(mTLS): load the trust roots and the optional client identity.
pub fn upstream_config(
    ca_path: Option<&str>,
    client_cert: Option<(&str, &str)>,
) -> anyhow::Result<Arc<rustls::ClientConfig>> {
    let _ = (ca_path, client_cert);
    todo!("mTLS: build a rustls ClientConfig with trust roots + optional client cert")
}

/// Wrap a finished `ServerConfig` in a Tokio TLS acceptor (plumbing).
pub fn acceptor(config: Arc<rustls::ServerConfig>) -> tokio_rustls::TlsAcceptor {
    tokio_rustls::TlsAcceptor::from(config)
}
