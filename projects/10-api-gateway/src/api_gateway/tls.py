"""mTLS scaffolding (security horizontal).

The gateway terminates TLS from clients and can, for a mutually-authenticated
data path, present a client certificate to its upstreams and verify theirs. Every
trust root and key path comes from config — never hard-coded. See SPEC.md
(Security -> mTLS), and the `TODO(mTLS)` in `main.py` for where these get wired.

Python's TLS is `ssl` in the standard library, so unlike the Rust version there is
no TLS crate to add: the work is entirely in **what you assert on the context**,
which is also where the mistakes live.

## The three traps in `ssl`

**`Purpose` reads backwards.** `ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)`
builds the context a **server** uses — the purpose names *who is being
authenticated by this context's peer*, not who is holding it. A server context
built with `Purpose.SERVER_AUTH` will look like it works, because most of TLS
still functions; what silently differs is the default verification and cipher
posture. Read the constant as "this context is used to authenticate a client to
me" and it stops being confusing.

**Ordering matters when relaxing settings.** `check_hostname = True` (the default
for `SERVER_AUTH`) is incompatible with `verify_mode = ssl.CERT_NONE`, and Python
raises if you set them in the wrong order. That is a feature: it is very hard to
accidentally disable verification without noticing. Do not "fix" that exception by
reordering the lines — for an upstream context, `check_hostname` and
`CERT_REQUIRED` are precisely the two things mTLS exists to guarantee.

**Loading a CA does not require a client certificate.** `load_verify_locations`
only teaches the context whom to *trust*; without `verify_mode = CERT_REQUIRED` a
server still accepts clients that present nothing at all. Edge mTLS is two
settings, and only one of them is the obvious one.
"""

from __future__ import annotations

import ssl

__all__ = ["server_context", "upstream_context"]


def server_context(cert_path: str, key_path: str, client_ca: str | None = None) -> ssl.SSLContext:
    """Build the `SSLContext` used to terminate TLS from clients.

    When `client_ca` is set, client certificates are **required and verified**
    against it — mTLS at the edge, so only holders of a certificate signed by that
    CA may connect at all. When it is `None`, this is ordinary one-way TLS.

    TODO(mTLS): create the context for `ssl.Purpose.CLIENT_AUTH`, load the chain
    and private key with `load_cert_chain`, and when `client_ca` is given, call
    `load_verify_locations` **and** set `verify_mode = ssl.CERT_REQUIRED` — see
    the module docstring on why the first without the second verifies nothing.
    Consider also pinning a `minimum_version` of `ssl.TLSVersion.TLSv1_2`.

    Note the wiring: uvicorn does not accept an `SSLContext`. It takes
    `ssl_certfile` / `ssl_keyfile` / `ssl_ca_certs` / `ssl_cert_reqs` and builds
    its own equivalent internally, so the straightforward path in `main.py` is to
    pass the config values through. This function is what you reach for when you
    want to assert something uvicorn's parameters do not expose — a version floor,
    a cipher list, a CRL — and it is worth writing either way, because knowing
    what the context has to say is the actual lesson.
    """
    raise NotImplementedError("mTLS: build a server SSLContext, optionally requiring client certs")


def upstream_context(
    ca_path: str | None = None,
    client_cert: tuple[str, str] | None = None,
) -> ssl.SSLContext:
    """Build the `SSLContext` the gateway uses when talking to upstreams over TLS.

    Verifies the upstream's certificate against `ca_path` (or the system trust
    store when `None`) and, for mutual TLS, presents the gateway's own identity
    from `client_cert = (cert_path, key_path)`.

    TODO(mTLS): create the context for `ssl.Purpose.SERVER_AUTH`, load the trust
    roots, and load the client identity when one is configured. Leave
    `check_hostname` and `verify_mode` at their secure defaults — an upstream
    context that skips hostname verification is a machine-in-the-middle away from
    forwarding every authenticated request to somebody else's server.

    Hand the result to `httpx.AsyncClient(verify=...)` in `main.py`; httpx takes an
    `SSLContext` directly, so unlike the server side this one wires in as-is.
    """
    raise NotImplementedError(
        "mTLS: build an upstream SSLContext with trust roots + optional client cert"
    )
