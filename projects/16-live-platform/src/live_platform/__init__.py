"""Live streaming platform (Twitch-lite) — project 16 of the backend gauntlet.

The capstone: RTMP/WebRTC ingest → ABR transcode ladder → LL-HLS packaging →
edge delivery → realtime chat, run on k8s with autoscaling transcode workers.
See `SPEC.md`, and `main` for how the planes are wired together.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
