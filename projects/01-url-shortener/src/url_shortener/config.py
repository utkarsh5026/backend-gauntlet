"""Typed settings for the shortener.

Every field maps to a variable in `.env.example`, and the type annotation is the
parser: declaring `port: int` gets the env lookup, the string->int coercion, the
default, and a startup error naming the offending variable.
"""

from __future__ import annotations

from typing import Annotated

from common_config import BaseConfig
from pydantic import Field, field_validator, model_validator
from pydantic_settings import NoDecode

__all__ = ["Settings"]


class Settings(BaseConfig):
    port: int = 8080
    """HTTP port. The compose file publishes Postgres on 5401 and Redis on 6301."""

    log_level: str = "info"

    database_url: str = "postgres://shortener:shortener@localhost:5401/shortener"
    redis_url: str = "redis://localhost:6301/0"

    node_id: int = Field(default=1, ge=0, lt=1024)
    """This instance's Snowflake node id. MUST be unique per running instance."""

    api_keys: Annotated[set[str], NoDecode] = set()
    """Keys accepted on the write/stats endpoints, as `API_KEYS=a,b,c`.

    `NoDecode` is load-bearing. pydantic-settings treats any `set[...]`/`list[...]`
    field as "complex" and runs `json.loads` on the raw environment string *in the
    source*, before any validator sees it - so `API_KEYS=dev-secret-key` dies at
    startup with a `JSONDecodeError`. It hides well, because an empty `API_KEYS=`
    is skipped entirely and the server boots fine with no keys at all."""

    public_base_url: str = ""
    """Origin used to build the `short_url` in a create response.

    Defaults to `http://localhost:{port}`; set it to the real origin behind a
    proxy, otherwise every short link you hand out points at localhost."""

    db_pool_min: int = Field(default=2, ge=1)
    db_pool_max: int = Field(default=20, ge=1)
    """Bounded on purpose (the "bounded pool sized on purpose" checklist item).

    The ceiling is a property of Postgres, not of this process: every pooled
    connection is a backend process over there, so the sum across all replicas
    has to stay under `max_connections`."""

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, raw: object) -> object:
        """Accept `API_KEYS="a,b,c"` from the environment.

        Only ever sees the raw string because the field is annotated `NoDecode`
        - see the note on the field itself.
        """
        if not isinstance(raw, str):
            return raw
        return {part.strip() for part in raw.split(",") if part.strip()}

    @model_validator(mode="after")
    def _default_base_url(self) -> Settings:
        if not self.public_base_url:
            self.public_base_url = f"http://localhost:{self.port}"
        return self

    @property
    def base_url(self) -> str:
        """`public_base_url` without a trailing slash, ready to join with a slug."""
        return self.public_base_url.rstrip("/")
