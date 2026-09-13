"""Typed settings for the platform.

Every field maps to a variable in `.env.example`, and every one has a working
default so `make run` boots against the project's docker-compose deps with no
edits. The type annotation *is* the parser: `max_streams: int` gets the env
lookup, the coercion, the default, and a startup error that names the variable.

Two changes from the Rust, both of the same kind — a mistake that used to
surface late now fails at boot:

* **`ABR_LADDER` is parsed here.** The Rust hard-coded three rungs and left
  parsing as a TODO. A ladder is not a vertical, it is configuration, and a
  typo in it (a missing `@`, a duplicated `720p`) would otherwise surface as a
  stream that transcodes into renditions no playlist can reference. Here it is a
  `ValidationError` before the server binds a port.
* **`PART_SECS < SEGMENT_SECS` is enforced.** An LL-HLS part longer than its
  segment is not a tuning choice, it is an invalid playlist — and it would be
  found by a player refusing to play, long after the config was written.
"""

from __future__ import annotations

import re
from typing import Self

from common_config import BaseConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["DEFAULT_LADDER", "Rendition", "Settings", "parse_ladder"]

DEFAULT_LADDER = "1080p:1920x1080@6000,720p:1280x720@3000,480p:854x480@1200"
"""Source-ish down to a mobile-friendly floor: three rungs a player adapts between."""

_RUNG = re.compile(r"(?P<name>[a-z0-9]{1,16}):(?P<width>\d+)x(?P<height>\d+)@(?P<kbps>\d+)")
"""One rung, `name:WIDTHxHEIGHT@KBPS`.

The name is restricted to `[a-z0-9]` because it becomes a URL path segment
(`/live/{stream}/{rendition}/…`). Narrowing it here means no configured
rendition can ever contain a `/` or `..` — one less thing the traversal check on
the playback side has to be right about."""


class Rendition(BaseModel):
    """One rung of the adaptive-bitrate ladder a stream is transcoded into.

    The set of renditions a viewer's player can switch between; the master
    playlist lists these. Frozen, because a ladder is shared by every session
    that references it — nothing should be able to edit a rung out from under a
    stream that is already transcoding to it.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    """Label used in the HLS path, e.g. `"720p"`."""
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    bitrate_kbps: int = Field(gt=0)


def parse_ladder(spec: str) -> tuple[Rendition, ...]:
    """Parse `ABR_LADDER` into rungs, raising `ValueError` naming the bad one.

    A tuple rather than a list: the ladder is fixed for the process lifetime,
    and an immutable value is safe to hand to every session without copying.
    """
    rungs: list[Rendition] = []
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        match = _RUNG.fullmatch(token)
        if match is None:
            raise ValueError(
                f"bad ABR_LADDER rung {token!r}: expected name:WIDTHxHEIGHT@KBPS, "
                "e.g. 720p:1280x720@3000"
            )
        rungs.append(
            Rendition(
                name=match["name"],
                width=int(match["width"]),
                height=int(match["height"]),
                bitrate_kbps=int(match["kbps"]),
            )
        )
    if not rungs:
        raise ValueError("ABR_LADDER is empty: a stream needs at least one rendition")
    names = [rung.name for rung in rungs]
    if len(set(names)) != len(names):
        raise ValueError(f"ABR_LADDER repeats a rendition name: {names}")
    return tuple(rungs)


class Settings(BaseConfig):
    # --- HTTP ---
    port: int = Field(default=8080, gt=0, lt=65536)
    """One listener for the ingest webhook, playback, chat and the admin probes."""

    log_level: str = "info"

    node_id: str = "node-a"
    """Stable id for this pod, stamped on cross-pod chat messages so a pod can
    drop its own echoes (V4). In k8s, the pod name."""

    # --- Control plane: Postgres (V1) ---
    database_url: str = "postgres://live:live@localhost:5416/live"
    db_max_connections: int = Field(default=16, gt=0)
    """Per process. See the Python checklist on sizing this against uvicorn
    workers, pod replicas, and Postgres `max_connections` together."""

    max_streams: int = Field(default=1000, gt=0)
    """Cap on concurrently live streams this control plane admits."""

    # --- ABR ladder + segmenting (V1/V3) ---
    abr_ladder: str = DEFAULT_LADDER
    segment_secs: float = Field(default=4.0, gt=0)
    part_secs: float = Field(default=0.5, gt=0)

    # --- Transcode queue: NATS JetStream (V2) ---
    nats_url: str = "nats://localhost:4216"
    transcode_stream: str = "TRANSCODE"
    transcode_lease_secs: float = Field(default=60.0, gt=0)
    """The visibility timeout. See `workers.py` on what choosing it costs."""
    target_backlog_per_worker: int = Field(default=4, gt=0)
    max_transcode_replicas: int = Field(default=50, gt=0)

    # --- Edge (V3) ---
    packager_origin: str = "http://localhost:9000"

    # --- Chat bus: Redis pub/sub (V4) ---
    redis_url: str = "redis://localhost:6316/0"
    outbox_capacity: int = Field(default=256, gt=0)
    """Per-subscriber outbox bound — the slow-consumer policy's threshold."""

    # --- Background loops ---
    run_background: bool = False
    """Off by default so the bare scaffold boots without reaching a V1/V2/V4
    `NotImplementedError` at startup. Flip on once those verticals exist."""

    @field_validator("abr_ladder")
    @classmethod
    def _ladder_parses(cls, value: str) -> str:
        parse_ladder(value)
        return value

    @model_validator(mode="after")
    def _parts_fit_in_segments(self) -> Self:
        if self.part_secs >= self.segment_secs:
            raise ValueError(
                f"PART_SECS ({self.part_secs}) must be shorter than "
                f"SEGMENT_SECS ({self.segment_secs}): a part is a slice of a segment"
            )
        return self

    @property
    def ladder(self) -> tuple[Rendition, ...]:
        """`ABR_LADDER`, parsed. Already validated at construction, so this cannot raise."""
        return parse_ladder(self.abr_ladder)
