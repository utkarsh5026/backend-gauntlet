"""Typed settings for the packager.

There is no database and no service dependency here — the filesystem *is* the
source — so this is the smallest config in the repo: where the media lives, what
port to serve it on, and how long a segment should aim to be.

Every field maps to a variable in `.env.example`, and the type annotation is the
parser: declaring `port: int = 8080` gets you the env lookup, the string->int
coercion, the default, and a startup error naming the offending variable.

## Why `media_dir` is resolved, and why that is a security decision

`MEDIA_DIR` arrives as a string and is turned into a resolved absolute `Path`
*once*, at startup. That single `.resolve()` is what makes the SPEC's traversal
criterion checkable at all: "an asset can never escape MEDIA_DIR" is a statement
about two absolute paths, and you cannot make it about a relative one without
knowing the process's working directory at the moment of the check — which can
change under you. Resolve once, compare against the resolved root, and the
question has an answer. See `catalog.py` for the comparison itself.
"""

from __future__ import annotations

from pathlib import Path

from common_config import BaseConfig
from pydantic import Field, field_validator

__all__ = ["Settings"]

DEFAULT_PORT = 8080
DEFAULT_TARGET_SEGMENT_SECS = 6.0


class Settings(BaseConfig):
    port: int = Field(default=DEFAULT_PORT, gt=0, lt=65536)
    """Port the HTTP server binds."""

    media_dir: Path = Path("./media")
    """Root of the media library, scanned at startup.

    Layout: `MEDIA_DIR/<asset>/<rendition>.mp4` — e.g. `media/bbb/1080p.mp4`
    alongside `media/bbb/720p.mp4`, which is the two-rung ladder V4's ABR
    criterion needs."""

    target_segment_secs: float = Field(default=DEFAULT_TARGET_SEGMENT_SECS, gt=0)
    """What segment length the segmenter aims for, in seconds.

    A goal, never a law: V2 may not split a GOP to hit it, so real segments land
    near this number rather than on it. Six seconds is the common HLS/DASH
    default — long enough that per-segment HTTP overhead is noise, short enough
    that a player can change its mind about the bitrate reasonably often."""

    log_level: str = "info"

    @field_validator("media_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        """Pin the library root to one absolute path at startup.

        `strict=False` because the directory legitimately may not exist yet — the
        catalog creates it. What matters is that the *string* stops being a
        relative path here, so every later containment check has something fixed
        to compare against. See the module docstring."""
        return value.expanduser().resolve()
