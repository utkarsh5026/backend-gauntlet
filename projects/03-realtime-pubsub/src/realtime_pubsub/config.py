"""Typed settings for the pub/sub server.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` starts a single node. Types here are the parser:
declaring `port: int` gets the env lookup, the coercion, the default, and a
startup error naming the offending variable.

The Rust side reached for `common_config::parse_or(key, default)` once per
variable, plus a hand-written `FromStr` for the overflow policy. In Python the
annotation does all of that — `overflow_policy: OverflowPolicy` parses the env
string into the enum and rejects an unknown value with a message naming the
field and listing the valid ones.
"""

from __future__ import annotations

from common_config import BaseConfig
from pydantic import Field, SecretStr

from .backpressure import OverflowPolicy

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- HTTP listener (the WebSocket endpoint is GET /ws and upgrades here) ---
    port: int = 8080
    log_level: str = "info"

    node_id: str = "node-a"
    """A stable id for THIS node, stamped onto every message put on the
    cross-node bus so a node can recognise and drop its own echoes (V4)."""

    # --- Multi-node fan-out (V4) ---
    cluster: bool = False
    """`false` runs a single in-process hub (V1-V3) and never touches the bus."""
    redis_url: str = "redis://localhost:6303/0"
    """Resolved even in single-node mode so `/debug/health` can probe the bus
    for reachability independently of whether we are bridging through it."""

    # --- WebSocket upgrade auth (security) ---
    ws_auth_token: SecretStr = SecretStr("")
    """Required shared secret on `GET /ws?token=...`. A browser WebSocket cannot
    set custom headers on the handshake, so the token rides the query string.

    Unset (empty) means every upgrade is rejected — **fail closed**, not "auth
    disabled". `SecretStr` so a stray `log.info(settings=cfg)` prints
    `**********`; the repo rule is never log secrets, and this makes it
    structural rather than a thing you have to remember."""

    # --- Per-connection backpressure (V2) ---
    outbox_capacity: int = Field(default=64, gt=0)
    """How many queued-but-unsent messages one slow client may buffer before the
    overflow policy kicks in. Small on purpose: a slow consumer must not let the
    broadcaster accumulate unbounded memory."""
    overflow_policy: OverflowPolicy = OverflowPolicy.DROP_OLDEST
    """What to do when a client's outbox is full."""

    # --- Presence heartbeat + TTL (V3) ---
    presence_ttl_secs: float = Field(default=30.0, gt=0)
    """A member not refreshed within this window is presumed gone and swept.
    Keep it a healthy multiple of the client's heartbeat interval so one missed
    beat does not evict a live member."""
    presence_sweep_interval_secs: float = Field(default=10.0, gt=0)
    """How often the background sweep task checks for expired members."""

    # --- Admin-panel roster DB (OPTIONAL - playground scaffolding, not the SPEC) ---
    database_url: str = ""
    """The people/groups/memberships behind the frontend admin panel. The
    pub/sub core (V1-V4) is store-free and runs WITHOUT this — empty disables
    the `/admin` API. Host port 5403 per the repo convention (postgres -> 54NN)."""

    @property
    def admin_enabled(self) -> bool:
        return bool(self.database_url)
