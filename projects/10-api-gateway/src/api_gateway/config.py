"""Typed settings and the route -> upstream table.

Two kinds of configuration meet here, and it is worth keeping them apart in your
head:

**Process settings** (`Settings`) come from the environment: what port to listen
on, how long to wait for an upstream, where the TLS material lives. Every field
maps to a variable in `.env.example`, and the type annotation *is* the parser —
declaring `port: int = 8080` gets you the lookup, the string->int coercion, the
default, and a startup error naming the offending variable.

**The route table** (`GatewayConfig`) comes from a JSON file, because it is data
with structure: a list of match rules, each pointing at a pool. This is the same
split every real gateway makes — Envoy's bootstrap flags versus its route
configuration — and it exists because the two change on completely different
schedules. You restart to change a port. You want to reload a route table without
dropping a request, which is exactly what V2's last criterion asks for.

All of this is *plumbing* and is implemented. The interesting parts are the
router (V2), balancer (V3) and health/circuit layer (V4) that consume the table.

## Two Python details worth knowing

**Milliseconds in, seconds out.** The environment speaks `..._MS` because that is
the readable unit for a timeout; asyncio and httpx speak seconds as floats,
everywhere. Converting once, here, in a derived property means no call site ever
holds a number whose unit it has to remember — the class of bug that produces a
2-microsecond deadline and a gateway that 504s everything.

**`UPSTREAM_BACKENDS` stays a string.** It looks like it wants to be
`list[str]`, but pydantic-settings tries to *JSON-decode* any complex-typed field
straight out of the environment, and `127.0.0.1:9010,127.0.0.1:9011` is not JSON.
So it is a `str` field with a validator that runs the real parse at startup — a
typo fails the process immediately instead of surfacing later as a pool that
mysteriously has one backend in it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from common_config import BaseConfig
from pydantic import BaseModel, Field, field_validator

from .balancer import LbPolicy

__all__ = [
    "GatewayConfig",
    "RouteConfig",
    "Settings",
    "UpstreamConfig",
    "parse_backends",
]

DEFAULT_PORT = 8080


def parse_backends(raw: str) -> list[str]:
    """Parse a comma-separated `host:port` list into a pool.

    Strict about the empty case on purpose: a route with no backends can never
    serve anything, and finding that out at the first request — as a 503 with no
    explanation — is much worse than finding it out at startup.
    """
    backends = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if not backends:
        raise ValueError("no backends configured (want `host:port,host:port`)")
    return backends


# --- the route table ---------------------------------------------------------


class UpstreamConfig(BaseModel):
    """A pool of backends plus the load-balancing policy over them."""

    backends: list[str] = Field(min_length=1)
    """`host:port` of each backend in the pool."""

    lb: LbPolicy = LbPolicy.ROUND_ROBIN
    """Load-balancing policy (V3). Defaults to round-robin — the floor."""


class RouteConfig(BaseModel):
    """One route: a match rule (`host` + `path_prefix` + `methods`) and the pool
    that matching requests are forwarded to."""

    name: str
    """Human name, used in logs, metrics labels and `GET /admin/routes`."""

    host: str | None = None
    """Optional host constraint (the `Host` header must equal this). `None` = any."""

    path_prefix: str
    """Longest-prefix match target, e.g. `/api/v2` (V2)."""

    methods: list[str] = Field(default_factory=list)
    """Allowed methods (`["GET", "POST"]`). Empty = any method."""

    upstream: UpstreamConfig
    """The pool of backends and how to balance across them."""

    @field_validator("methods")
    @classmethod
    def _upper(cls, methods: list[str]) -> list[str]:
        """Normalize to the uppercase form HTTP actually uses.

        Done at load time so the matcher on the hot path can compare strings
        directly instead of calling `.upper()` on every request — and so a config
        that says `get` behaves the same as one that says `GET`, rather than
        matching nothing at all and looking like a routing bug.
        """
        return [m.strip().upper() for m in methods if m.strip()]


class GatewayConfig(BaseModel):
    """The whole gateway config: an ordered list of routes.

    "Ordered" is how it arrives, not how it must be matched — V2 requires
    longest-prefix precedence that does *not* depend on the order routes were
    written in.
    """

    routes: list[RouteConfig] = Field(min_length=1)

    @classmethod
    def load(cls, path: Path | str) -> Self:
        """Load a JSON route table from disk. See `gateway.example.json`."""
        raw = Path(path).read_text(encoding="utf-8")
        return cls.model_validate(json.loads(raw))

    @classmethod
    def demo(cls, backends: list[str], lb: LbPolicy = LbPolicy.ROUND_ROBIN) -> Self:
        """A built-in single catch-all route (`/` -> `backends`).

        Used when no `CONFIG_PATH` is set, so `make run` and docker-compose need
        zero files: the demo path in the SPEC has to work before you have written
        a config, or the first thing the project asks of you is bookkeeping.
        """
        return cls.model_validate(
            {
                "routes": [
                    {
                        "name": "default",
                        "path_prefix": "/",
                        "methods": [],
                        "upstream": {"backends": backends, "lb": lb},
                    }
                ]
            }
        )


# --- process settings --------------------------------------------------------


class Settings(BaseConfig):
    # --- listener ---

    port: int = Field(default=DEFAULT_PORT, gt=0, lt=65536)
    """Port the gateway listens on for client traffic, admin and metrics."""

    log_level: str = "info"

    # --- routing (V2) ---

    config_path: str = ""
    """Path to a JSON route table. Empty -> the built-in catch-all below."""

    upstream_backends: str = "127.0.0.1:9010"
    """Comma-separated `host:port` pool for the built-in catch-all route."""

    lb_policy: LbPolicy = LbPolicy.ROUND_ROBIN
    """Load-balancing policy for the built-in catch-all route (V3)."""

    # --- resilience / timeouts (V1 / V4) ---

    upstream_connect_timeout_ms: int = Field(default=2_000, gt=0)
    """Max time a single upstream TCP connect may take before it's a 502.

    Separate from the overall deadline because the two failures are different:
    a refused or unroutable backend should be *fast*, and lumping it in with the
    request deadline makes a dead node cost the same as a slow one."""

    request_timeout_ms: int = Field(default=10_000, gt=0)
    """Overall per-request deadline (connect + upstream response) before a 504.

    The number the whole gateway's tail latency is bounded by. Set it too high and
    one hung upstream holds a connection and a task for that long, times every
    in-flight request; too low and you kill legitimate slow downloads."""

    health_probe_ms: int = Field(default=2_000, gt=0)
    """Active health-check probe interval (V4)."""

    circuit_failure_threshold: int = Field(default=5, gt=0)
    """Consecutive upstream failures before a backend's circuit opens (V4)."""

    circuit_open_cooldown_ms: int = Field(default=5_000, gt=0)
    """How long a circuit stays open before admitting a half-open trial (V4)."""

    # --- edge limits (security) ---

    max_body_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    """Reject a request body larger than this many bytes (413).

    Enforced while *streaming*, not from `Content-Length`: the header is a claim
    by the client, and a chunked request need not send one at all."""

    # --- mTLS (security, tls.py) — all optional; plain HTTP when unset ---

    tls_cert: str = ""
    tls_key: str = ""
    """Server cert + key the gateway presents to clients (TLS termination)."""

    tls_client_ca: str = ""
    """CA to verify *client* certs against — set to require mTLS at the edge."""

    upstream_tls_ca: str = ""
    upstream_client_cert: str = ""
    upstream_client_key: str = ""
    """Trust root and client identity for mutual TLS to upstreams."""

    @field_validator("upstream_backends")
    @classmethod
    def _backends_parse(cls, raw: str) -> str:
        """Fail at startup, not at the first proxied request, on a malformed pool."""
        parse_backends(raw)
        return raw

    # --- derived ---

    @property
    def backends(self) -> list[str]:
        """The catch-all pool, parsed."""
        return parse_backends(self.upstream_backends)

    @property
    def connect_timeout(self) -> float:
        return self.upstream_connect_timeout_ms / 1000.0

    @property
    def request_timeout(self) -> float:
        return self.request_timeout_ms / 1000.0

    @property
    def health_probe_interval(self) -> float:
        return self.health_probe_ms / 1000.0

    @property
    def circuit_open_cooldown(self) -> float:
        return self.circuit_open_cooldown_ms / 1000.0

    @property
    def tls_enabled(self) -> bool:
        """Whether to terminate TLS. Both halves of the keypair or neither —
        a cert with no key is a typo, and silently serving plain HTTP because of
        one is how a "TLS-terminating" gateway ends up not terminating TLS."""
        return bool(self.tls_cert and self.tls_key)

    def gateway_config(self) -> GatewayConfig:
        """The route table: an explicit JSON file, or the built-in catch-all."""
        if self.config_path:
            return GatewayConfig.load(self.config_path)
        return GatewayConfig.demo(self.backends, self.lb_policy)
