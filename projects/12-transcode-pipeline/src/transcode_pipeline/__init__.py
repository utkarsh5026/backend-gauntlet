"""Distributed transcoding pipeline — project 12 of the backend gauntlet.

`ffmpeg input.mov output.mp4` turned into a distributed system: cut a source at
keyframes (V1), schedule the work as a durable DAG (V2), transcode the chunks in
parallel and idempotently (V3), and stitch them back without a seam (V4). See
`SPEC.md`, and `main` for how the pieces are wired together.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
