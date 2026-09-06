"""Shared fixtures.

Every fixture here builds a **real media directory on disk**, because the catalog
is a directory scanner and there is nothing honest to fake about it: the thing
under test is what `Path.glob` finds, what it names the renditions, and what it
does with a directory that has no `.mp4` in it.

The files themselves are junk bytes, deliberately. Nothing in the scaffold parses
a source — `demux` raises — so a real MP4 would buy nothing at this stage and cost
a binary fixture in the repo. V1's tests are a different matter: they need a
small, committed, *known* MP4, and the SPEC's Proof line names what it has to
prove about it.

`client` drives the whole app through `httpx.ASGITransport` — the same ASGI
interface uvicorn uses, so the tests stay genuinely async (an `await` bug in your
code shows up as one) without `TestClient`'s sync-portal indirection. Entering
`lifespan_context` runs the real startup path, so a test can never pass against
wiring that would fail in production.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

from vod_streaming.config import Settings
from vod_streaming.main import create_app

RENDITIONS = ("720p", "1080p")
"""Two rungs, because one rendition cannot demonstrate a ladder and V4's ABR
criterion is about switching between them."""


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    """A library with one asset at two renditions, plus two things to ignore.

    The junk entries are not padding: a `.txt` beside the sources and an asset
    directory with nothing playable in it are both states a real library ends up
    in, and both should be invisible in `GET /assets` rather than a crash or a
    phantom rendition.
    """
    root = tmp_path / "media"
    asset = root / "bbb"
    asset.mkdir(parents=True)
    for name in RENDITIONS:
        (asset / f"{name}.mp4").write_bytes(b"\x00\x00\x00\x18ftypiso6" + b"\x00" * 128)
    (asset / "notes.txt").write_text("not media")
    (root / "empty-title").mkdir()
    return root


@pytest.fixture
def settings(media_dir: Path) -> Settings:
    return Settings(port=8080, media_dir=media_dir, target_segment_secs=6.0)


@pytest.fixture
async def client(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://vod.test") as http:
            yield http
