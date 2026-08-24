"""Typed settings for one account.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` starts an account seeded with the bootstrap
credentials and nothing else.

Two fields are `SecretStr`, and that is not decoration. `SecretStr.__repr__`
renders as `**********`, so a stray `log.info("config", cfg=settings)` — the most
common way a secret ends up in a log aggregator forever — prints nothing useful.
The SPEC grades "no secret reaches a log line" with a test; this makes passing it
the default rather than a thing you have to remember.
"""

from __future__ import annotations

from pathlib import Path

from common_config import BaseConfig
from pydantic import Field, SecretStr

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- server ---
    port: int = 9025
    authz_port: int = 9026
    # Loopback by default: the authorization endpoint is an internal contract
    # between this service and projects 23/24/06, not a public API.
    authz_host: str = "127.0.0.1"
    log_level: str = "info"

    # --- account identity ---
    account_id: str = "000000000000"
    aws_region: str = "us-east-1"
    aws_partition: str = "aws"

    # --- bootstrap credentials (the account root) ---
    bootstrap_access_key_id: str = "AKIAIOSFODNN7EXAMPLE"
    bootstrap_secret_access_key: SecretStr = SecretStr("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")

    # --- SigV4 (V1) ---
    sigv4_clock_skew_seconds: float = Field(default=300.0, gt=0)
    presign_max_expiry_seconds: float = Field(default=604_800.0, gt=0)
    signing_key_cache_size: int = Field(default=1024, gt=0)

    # --- policy limits (V2) ---
    max_policy_bytes: int = Field(default=6144, gt=0)
    max_statements_per_policy: int = Field(default=100, gt=0)
    max_policies_per_principal: int = Field(default=10, gt=0)
    max_condition_keys_per_statement: int = Field(default=32, gt=0)

    # --- STS (V4) ---
    session_token_key: SecretStr = SecretStr("dev-only-session-token-key-replace-me")
    default_session_duration_seconds: float = Field(default=3600.0, gt=0)
    max_session_duration_seconds: float = Field(default=43_200.0, gt=0)
    # Chaining truncates you regardless of what you asked for. Kept separate from
    # the max above because they are different rules, not one rule with a min().
    chained_session_max_duration_seconds: float = Field(default=3600.0, gt=0)
    max_role_chain_depth: int = Field(default=2, ge=1)

    # --- authorizer hot path (V5) ---
    decision_cache_size: int = Field(default=100_000, gt=0)
    # A security parameter wearing a performance parameter's clothes: this is the
    # maximum time a revoked permission keeps working.
    decision_cache_ttl_seconds: float = Field(default=1.0, gt=0)
    compiled_policy_cache_size: int = Field(default=4096, gt=0)

    # --- audit & revocation (V6) ---
    audit_log_path: Path = Path("./run/audit.log")
    audit_queue_size: int = Field(default=10_000, gt=0)
    audit_flush_interval_seconds: float = Field(default=1.0, gt=0)
    session_reap_interval_seconds: float = Field(default=30.0, gt=0)

    @property
    def root_arn(self) -> str:
        """The account root principal — what the bootstrap credentials are."""
        return f"arn:{self.aws_partition}:iam::{self.account_id}:root"

    @property
    def authz_address(self) -> str:
        """Where projects 23/24/06 point their authorization client."""
        return f"{self.authz_host}:{self.authz_port}"
