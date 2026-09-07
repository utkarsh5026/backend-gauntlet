"""Shared fixtures.

Every fixture hands out a store rooted in a throwaway directory, because the
filesystem *is* the database here: sharing one root between tests would let a
blob committed by one appear in another's dedup check or GC sweep, and the
failures would be order-dependent and maddening.

The app is driven through `httpx.ASGITransport` rather than Starlette's
`TestClient`. `TestClient` runs the app on its own event loop in a worker
thread, which hides exactly the bug this project is most likely to have — a
blocking call on the loop — and makes the lifespan's background tasks awkward to
reason about. `ASGITransport` runs the real app on the test's own loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from object_store.config import BlobLayoutKind, Settings
from object_store.index import Index
from object_store.index_backend import LocalIndex
from object_store.lifecycle import Lifecycle
from object_store.main import build_state, create_app
from object_store.multipart import Multipart
from object_store.state import AppState
from object_store.store import Store


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A fresh data root, wiped when the test ends."""
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """Settings pointing at the throwaway root, with auth and CDC off.

    `_env_file=None` so a developer's real `.env` — which may well set
    `SECRET_ACCESS_KEY` — cannot reach in and gate every test behind auth.
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=data_dir,
        secret_access_key="",
        index_url="",
    )


@pytest.fixture
def store(data_dir: Path) -> Store:
    return Store(data_dir, layout=BlobLayoutKind.FILE_CAS)


@pytest.fixture
def haystack_store(data_dir: Path) -> Store:
    """A store whose write policy packs small blobs into volumes.

    A small volume cap so tests can cross it without writing megabytes — the
    fallback-to-FileCas path only exists above the cap, and it needs exercising.
    """
    return Store(data_dir, layout=BlobLayoutKind.HAYSTACK, max_volume_size=64 * 1024)


@pytest.fixture
def index(data_dir: Path, store: Store) -> Index:
    """An index whose GC grace window is zero.

    In production a blob must sit unreferenced for a minute before it can be
    reclaimed. Waiting that out in a test is not a test, it is a nap — and the
    tmp-scan, which is the *other* half of the in-flight-PUT guard, is exactly
    what a zero grace window forces the tests to actually exercise.
    """
    return Index(data_dir, store, gc_grace=0.0)


@pytest.fixture
def multipart(data_dir: Path, store: Store, index: Index) -> Multipart:
    return Multipart(data_dir, store, LocalIndex(index))


@pytest.fixture
def lifecycle(store: Store, index: Index, multipart: Multipart) -> Lifecycle:
    return Lifecycle(LocalIndex(index), store, multipart)


@pytest.fixture
def app_state(settings: Settings) -> AppState:
    """The same state the `client` app serves from.

    A fixture rather than something dug out of the client, so a test that needs
    to reach past HTTP — shrink a cap, inspect the store — has a typed handle
    instead of poking at httpx internals.
    """
    return build_state(settings)


@pytest_asyncio.fixture
async def client(settings: Settings, app_state: AppState) -> AsyncIterator[AsyncClient]:
    """The real app over an in-process transport, lifespan and all.

    `LifespanManager` is not used because httpx's `ASGITransport` does not run
    the lifespan; the app state is built here explicitly instead, which also
    keeps the background sweepers out of the tests. They have their own
    deterministic tests that drive one pass with an injected clock — letting
    them run on a timer under the suite would make every other test racy.
    """
    app = create_app(settings)
    app.state.app_state = app_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://store") as http:
        yield http
    app_state.store.close()
