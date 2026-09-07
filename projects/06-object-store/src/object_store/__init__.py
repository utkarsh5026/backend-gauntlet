"""An S3-compatible object store built on plain files (project 06).

The public surface is small on purpose: `create_app` for the ASGI app,
`Settings` for configuration, and `AppState` for what a handler can reach. The
verticals — `store`, `streaming`, `index`, `multipart` — are imported from their
own modules, because the SPEC maps a challenge to a module and collapsing them
into one namespace would blur that mapping.
"""

from __future__ import annotations

from .config import DEFAULT_MAX_OBJECT_SIZE, BlobLayoutKind, Settings
from .state import AppState

__all__ = [
    "DEFAULT_MAX_OBJECT_SIZE",
    "AppState",
    "BlobLayoutKind",
    "Settings",
    "create_app",
]


def create_app(settings: Settings | None = None):  # noqa: ANN201
    """Build the ASGI app.

    Deferred to call time rather than imported at module level: `main` pulls in
    uvicorn and the whole route tree, and a test that only wants `Digest` should
    not pay for that.
    """
    from .main import create_app as _create_app

    return _create_app(settings)
