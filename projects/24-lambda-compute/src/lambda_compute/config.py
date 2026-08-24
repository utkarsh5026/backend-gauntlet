"""Typed settings for one node.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` starts a node with no functions registered.
"""

from __future__ import annotations

from pathlib import Path

from common_config import BaseConfig
from pydantic import Field

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- server ---
    port: int = 9001
    runtime_api_port: int = 9002
    # Loopback by default. An execution environment reaching the network is a
    # finding, not a feature — see V3.
    runtime_api_host: str = "127.0.0.1"
    log_level: str = "info"

    # --- function defaults (V1/V2) ---
    default_memory_mb: int = Field(default=128, gt=0)
    default_timeout_seconds: float = Field(default=3.0, gt=0)
    max_sync_payload_bytes: int = Field(default=6_291_456, gt=0)
    max_async_payload_bytes: int = Field(default=262_144, gt=0)

    # --- execution environments (V2) ---
    environment_idle_ttl_seconds: float = Field(default=300.0, gt=0)
    max_environments: int = Field(default=256, gt=0)
    init_timeout_seconds: float = Field(default=10.0, gt=0)

    # --- sandbox (V3) ---
    sandbox_root: Path = Path("./run")
    sandbox_tmp_mb: int = Field(default=512, gt=0)

    # --- concurrency (V4) ---
    account_concurrency_limit: int = Field(default=1000, gt=0)
    # Not the limit but the *rate* — this is the number that shapes a cold front.
    scale_up_rate_per_second: float = Field(default=500.0, gt=0)
    burst_concurrency: int = Field(default=500, gt=0)

    # --- async invocation (V5) ---
    async_queue_size: int = Field(default=10_000, gt=0)
    # Attempts including the first: Lambda's "2 retries" is 3 attempts.
    async_max_attempts: int = Field(default=3, ge=1)
    async_retry_base_seconds: float = Field(default=1.0, gt=0)
    async_max_event_age_seconds: float = Field(default=21_600.0, gt=0)

    # --- event source mapping (V6) ---
    event_source_url: str = "http://localhost:8000"
    event_source_batch_size: int = Field(default=100, gt=0)
    event_source_batch_window_seconds: float = Field(default=1.0, gt=0)
    event_source_parallelisation: int = Field(default=1, ge=1)

    @property
    def runtime_api_address(self) -> str:
        """What a sandbox is handed as `AWS_LAMBDA_RUNTIME_API`.

        Host:port with no scheme — that is the shape the real variable has, and
        runtimes written against it concatenate their own `http://`.
        """
        return f"{self.runtime_api_host}:{self.runtime_api_port}"
