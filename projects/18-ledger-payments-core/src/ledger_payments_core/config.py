"""Typed settings for the ledger.

Every field maps to a variable in `.env.example`, and every one has a working
default, so `make run` boots against this project's docker-compose Postgres and
Redis with no edits. The type annotation *is* the parser: `max_transfer_minor: int`
gets the env lookup, the coercion, the default, and a startup error that names the
variable.

## Secrets are a type, not a convention

`API_KEYS` and `WEBHOOK_SIGNING_SECRET` are `SecretStr`. Its `repr` is
`SecretStr('**********')`, so a settings object that lands in a log line, in a
traceback's locals, or in an error body carries the mask rather than the key — the
Python equivalent of the Rust side's hand-written redacting `Debug`. Reading the
real value is an explicit `.get_secret_value()`, which gives an audit one thing to
grep for.

The intervals keep their `.env.example` names and units (`*_MS`); the `*_secs`
properties convert once, here, because every asyncio and httpx API takes seconds.
"""

from __future__ import annotations

from common_config import BaseConfig
from pydantic import Field, SecretStr

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- HTTP ---
    port: int = Field(default=8080, gt=0, lt=65536)
    log_level: str = "info"

    # --- Postgres: the transactional source of truth ---
    database_url: str = "postgres://ledger:ledger@localhost:5418/ledger"
    """Carries a password — never log it."""
    db_max_connections: int = Field(default=20, gt=0)
    """Per process. A transfer holds its connection for the whole money-moving
    transaction *and every serialization retry of it*, so under contention this
    bound, not the CPU, is usually what caps transfers/sec. Size it together with the
    number of server processes and Postgres' own `max_connections` (the Python
    checklist)."""

    # --- Redis: the idempotency response cache (V3) ---
    redis_url: str = "redis://localhost:6318"
    redis_max_connections: int = Field(default=32, gt=0)
    """redis-py's pool is unbounded unless told otherwise; an unbounded pool under a
    retry storm turns into file-descriptor exhaustion rather than backpressure."""

    # --- Security ---
    api_keys: SecretStr = SecretStr("dev-key-change-me")
    """Comma-separated, so a key can be rotated by serving the old and new together."""
    max_transfer_minor: int = Field(default=100_000_000, gt=0)
    """Ceiling on one transfer, in minor units (100000000 = $1,000,000.00)."""

    # --- Idempotency (V3) ---
    idempotency_ttl_secs: int = Field(default=86_400, gt=0)
    """How long a stored result stays replayable. Past it, the same key is a new
    request — that window is a documented decision (`docs/18-design.md`)."""

    # --- Isolation / retries (V2) ---
    max_serialization_retries: int = Field(default=5, ge=0)
    """How many times a conflicted transfer is retried before it is rejected
    cleanly. Bounded on purpose: an unbounded retry loop is a livelock waiting for
    enough contention."""

    # --- Webhooks (V4) ---
    run_dispatcher: bool = False
    """Off by default so the bare scaffold serves the API without the dispatcher's
    first outbox claim reaching an unbuilt V4 function."""
    webhook_signing_secret: SecretStr = SecretStr("whsec_change_me")
    webhook_endpoint_url: str = "http://localhost:9000/webhooks"
    """A single sink for the scaffold; a real system stores an endpoint per merchant."""
    webhook_max_attempts: int = Field(default=8, gt=0)
    """Deliveries before an event is dead-lettered."""
    webhook_dispatch_interval_ms: int = Field(default=1000, gt=0)
    webhook_dispatch_batch: int = Field(default=50, gt=0)
    """Events claimed per dispatch round."""
    webhook_timeout_ms: int = Field(default=5000, gt=0)
    """Per delivery. An endpoint that accepts the connection and never answers must
    cost one timeout, not a stalled dispatcher."""

    @property
    def api_key_list(self) -> tuple[SecretStr, ...]:
        """The configured keys, split on commas and stripped; blanks dropped."""
        raw = self.api_keys.get_secret_value()
        return tuple(SecretStr(key.strip()) for key in raw.split(",") if key.strip())

    @property
    def webhook_dispatch_interval_secs(self) -> float:
        return self.webhook_dispatch_interval_ms / 1000

    @property
    def webhook_timeout_secs(self) -> float:
        return self.webhook_timeout_ms / 1000
