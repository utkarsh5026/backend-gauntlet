"""Typed settings for the ingest server.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` binds both planes on localhost. The annotation *is*
the parser: `target_part_secs: float` gets the env lookup, the coercion, the
default and a startup error naming the offending variable — so
`TARGET_PART_SECS=0.3s` fails at boot instead of quietly becoming the default.

The knobs group by what they are a lever on, and in this project nearly all of
them are **latency** levers: the part target sets how far behind the live edge a
player can sit, the segment target sets how far back a new viewer joins, and the
window sets how much of the broadcast is still fetchable — which is also, not by
coincidence, the memory bound. RAM tracks `live_window_segments`, never airtime.
"""

from __future__ import annotations

from typing import Self

from common_config import BaseConfig
from pydantic import Field, model_validator

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- The two planes ---
    rtmp_port: int = Field(default=1935, ge=0, lt=65536)
    """Raw-TCP port broadcasters publish to (`rtmp://host:1935/live/<key>`).

    `0` means "let the kernel pick", which is what the tests use so several
    servers can run in one process. Read the bound port off
    `RtmpIngest.port` rather than off this field."""

    http_port: int = Field(default=8080, gt=0, lt=65536)
    """LL-HLS delivery: playlists, init, segments, parts, `/healthz`, `/metrics`."""

    log_level: str = "info"

    # --- Security: who may publish ---
    stream_keys: str = ""
    """Comma-separated allow-list, exactly as it appears in the environment.

    Kept as the raw string and split by `allowed_keys` below, rather than
    declared `list[str]`: pydantic-settings parses a `list` field from the
    environment as **JSON**, so `STREAM_KEYS=a,b` would be a startup error and
    `STREAM_KEYS=["a","b"]` is not what anyone types. Empty means *any key may
    publish* — dev only, and `main` says so loudly at startup."""

    # --- LL-HLS cadence (V3 cuts on these, V4 advertises them) ---
    target_part_secs: float = Field(default=0.3, gt=0)
    """Part target, ~0.2–0.35 s. Smaller is lower latency and more requests —
    every held blocking reload wakes once per part."""

    target_segment_secs: float = Field(default=4.0, gt=0)
    """Segment target. LL-HLS segments can stay a few seconds long because parts
    do the latency work; a segment only has to start on a keyframe."""

    live_window_segments: int = Field(default=8, gt=0)
    """Finished segments kept fetchable. This is the memory bound — see the
    module docstring."""

    # --- Python-specific: the ingest socket's read buffer ---
    rtmp_read_buffer_bytes: int = Field(default=64 * 1024, ge=4096)
    """`limit` for each publisher's `asyncio.StreamReader`.

    It is the backpressure knob, and it is not obvious that it is one: the
    stream transport **pauses reading** from the socket once more than twice
    this many bytes sit unread in the reader. So a bursty publisher that
    outruns your parser fills its *own* TCP receive window and its encoder
    slows down, instead of filling your heap. Too small and a keyframe arrives
    in more wakeups than it needs to; too large and a stalled parser holds more
    of a hostile peer's bytes in memory before TCP pushes back."""

    @model_validator(mode="after")
    def _segment_holds_a_part(self) -> Self:
        # A segment shorter than one part cannot be made of parts. Caught here
        # so the error names both variables instead of surfacing as a playlist
        # with zero parts per segment and a player that never starts.
        if self.target_segment_secs < self.target_part_secs:
            raise ValueError("TARGET_SEGMENT_SECS must be >= TARGET_PART_SECS")
        return self

    @property
    def allowed_keys(self) -> frozenset[str]:
        """`STREAM_KEYS` split, trimmed, and with empty entries dropped.

        A `frozenset` because the only question ever asked of it is membership,
        and because it is shared by every session for the life of the process —
        immutable is the honest type for that.
        """
        return frozenset(key.strip() for key in self.stream_keys.split(",") if key.strip())
