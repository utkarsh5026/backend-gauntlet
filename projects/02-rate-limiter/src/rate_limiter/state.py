"""The objects the gRPC service and the admin routes both need.

Its own module so `service` and `routes` can depend on the shape without
importing `main` (which imports them — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

from .config import Settings
from .redis_limiter import RedisLimiter

__all__ = ["AppState"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    redis: Redis
    limiter: RedisLimiter
