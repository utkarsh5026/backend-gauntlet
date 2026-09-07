"""The app's own wiring: startup, the background loops, and graceful shutdown."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from object_store.config import Settings
from object_store.main import create_app


async def test_the_lifespan_opens_the_store_and_drains_cleanly(
    settings: Settings,
) -> None:
    """The container check in miniature: start, serve, stop, no stray tasks.

    A background task that outlives the lifespan is the bug this catches — it
    keeps running against a closed store, and in a container it is what turns
    `docker stop` into a ten-second SIGKILL instead of a clean exit.
    """
    app = create_app(settings)
    before = len(asyncio.all_tasks())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://store") as client:
        async with app.router.lifespan_context(app):
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.put("/photos")).status_code == 200
            assert (await client.put("/photos/a.txt", content=b"hello")).status_code == 200

    # Give the cancellations a tick to settle before counting.
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) <= before + 1


async def test_the_data_layout_is_created_on_open(settings: Settings) -> None:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        for name in ("objects", "volumes", "tmp", "quarantine", "index", "uploads"):
            assert (settings.data_dir / name).is_dir(), name


async def test_state_is_required_before_a_handler_runs(settings: Settings) -> None:
    """Nothing may resolve state at import time, before the lifespan built any."""
    from object_store.state import get_state

    class _FakeRequest:
        class app:  # noqa: N801
            class state:  # noqa: N801
                pass

    with pytest.raises(RuntimeError):
        get_state(_FakeRequest())  # type: ignore[arg-type]


async def test_the_scrubber_parks_on_an_empty_store(settings: Settings) -> None:
    """An empty store must not burn a core re-scanning nothing."""
    from object_store.main import build_state

    state = build_state(settings)
    task = asyncio.create_task(state.store.run_scrubber(0.01))
    await asyncio.sleep(0.05)

    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    state.store.close()


async def test_a_commit_wakes_the_parked_scrubber(settings: Settings) -> None:
    """The commit-side notify is what makes a fresh object audited promptly."""
    from prometheus_client import REGISTRY

    from object_store.main import build_state

    state = build_state(settings)
    task = asyncio.create_task(state.store.run_scrubber(60.0))
    await asyncio.sleep(0.05)

    before = REGISTRY.get_sample_value("object_store_scrub_blobs_verified_total") or 0.0
    await state.store.commit_bytes(b"audit me")
    await asyncio.sleep(0.1)

    after = REGISTRY.get_sample_value("object_store_scrub_blobs_verified_total") or 0.0
    assert after > before

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    state.store.close()
