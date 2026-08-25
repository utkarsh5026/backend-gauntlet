"""Typed settings for one broker node.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` starts an empty broker with no queues and no
external dependencies.

Two groups of fields are worth reading as a pair. The `default_*` values are what
a **new queue** gets; the `max_*` values are what `SetQueueAttributes` is allowed
to set. Keeping them separate is deliberate — a default is a convenience and a
ceiling is a safety property, and collapsing them into one number is how a caller
ends up able to set a 30-day visibility timeout because somebody wanted a
friendlier default.
"""

from __future__ import annotations

from common_config import BaseConfig
from pydantic import Field

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- server ---
    port: int = 9029
    host: str = "0.0.0.0"
    log_level: str = "info"

    # --- account identity ---
    account_id: str = "000000000000"
    aws_region: str = "us-east-1"
    # What clients actually reach. Queue URLs are built from this, so a value
    # that only resolves inside the container produces URLs that work in your
    # tests and nowhere else.
    endpoint_host: str = "localhost:9029"

    # --- queue defaults (V6) ---
    default_visibility_timeout_seconds: float = Field(default=30.0, ge=0)
    default_receive_wait_time_seconds: float = Field(default=0.0, ge=0)
    default_delay_seconds: float = Field(default=0.0, ge=0)
    default_retention_seconds: float = Field(default=345_600.0, gt=0)

    # --- protocol limits ---
    max_message_bytes: int = Field(default=262_144, gt=0)
    max_batch_entries: int = Field(default=10, gt=0)
    max_receive_messages: int = Field(default=10, gt=0)
    max_receive_wait_time_seconds: float = Field(default=20.0, gt=0)
    max_visibility_timeout_seconds: float = Field(default=43_200.0, gt=0)
    max_delay_seconds: float = Field(default=900.0, ge=0)
    max_retention_seconds: float = Field(default=1_209_600.0, gt=0)
    min_retention_seconds: float = Field(default=60.0, gt=0)
    max_queue_name_length: int = Field(default=80, gt=0)

    # --- FIFO + dedup (V4, V5) ---
    dedup_window_seconds: float = Field(default=300.0, gt=0)
    # The hard cap on the window. Past this the send path needs a *defined*
    # behaviour — reject or evict — rather than growing until the process dies.
    max_dedup_entries: int = Field(default=1_000_000, gt=0)

    # --- quotas ---
    max_inflight_per_queue: int = Field(default=120_000, gt=0)
    max_inflight_per_fifo_queue: int = Field(default=20_000, gt=0)
    max_waiters: int = Field(default=20_000, gt=0)
    max_queues: int = Field(default=1_000, gt=0)

    # --- the deadline engine (V2) ---
    # The safety net, not a polling interval: the engine should sleep until the
    # *next deadline*. This bounds how long a missed wakeup goes unnoticed.
    timer_tick_seconds: float = Field(default=0.05, gt=0)
    # Without a per-tick bound, a tick with a million due deadlines is a stalled
    # server — the event loop does not get a turn until it finishes.
    max_deadlines_per_tick: int = Field(default=10_000, gt=0)

    # --- authentication (project 25) ---
    require_sigv4: bool = False
    iam_authz_url: str = "http://127.0.0.1:9026"

    def queue_url(self, name: str) -> str:
        """The URL a queue is addressed by.

        Opaque to clients by contract, even though it is obviously readable: the
        account and name are in there so a URL is unambiguous about ownership,
        but a client that *parses* one has coupled itself to a format the service
        is free to change.
        """
        return f"http://{self.endpoint_host}/{self.account_id}/{name}"

    def queue_arn(self, name: str) -> str:
        """The ARN a redrive policy and an IAM policy name the queue by."""
        return f"arn:aws:sqs:{self.aws_region}:{self.account_id}:{name}"
