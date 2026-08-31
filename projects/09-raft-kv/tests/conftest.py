"""Shared fixtures.

The acceptance tests for V1-V4 are yours to write (see the SPEC's "Proof" lines).
What lives here is only the harness.

Note the client: `httpx.AsyncClient` over `ASGITransport` rather than Starlette's
`TestClient`. It exercises the app through the same ASGI interface uvicorn uses,
keeps the tests genuinely async (so an `await` bug in your code shows up as one),
and avoids `TestClient`'s sync-portal indirection.

Note also `tmp_path`: every node gets its own `data_dir`. This node's entire
durable identity — its term, its vote, its log — is that directory, so a shared
one would let one test's persisted vote become another test's "recovered" state,
and the restart tests V1 asks for depend on controlling exactly what is on disk.

**When you come to write a real cluster test**, `single_node` is not the fixture
you want. Build three `Settings` sharing one `PEERS` string with different
`node_id`s, and give each its own `data_dir`. Two ways to run them: three real
uvicorn servers on the loopback ports in `PEERS` (slower, but exercises the actual
`PeerClient` and lets you test partitions by refusing connections), or one process
with a fake `PeerClient` that routes calls between in-memory nodes (much faster,
and lets you *delay* and *drop* messages deterministically — which is how you
reproduce the §5.4.2 scenario on purpose rather than by luck). The second is worth
the setup; consensus bugs live in orderings you cannot reach by timing alone.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

from raft_kv.config import Settings
from raft_kv.main import create_app


@pytest.fixture
def single_node(tmp_path: Path) -> Settings:
    """A one-node "cluster" over a throwaway data dir.

    A quorum of one, which is what makes the SPEC's "boring path" runnable: this
    node can commit by itself, so PUT/GET/DELETE can be made to round-trip before
    a second process exists. The timings are tightened well below the defaults so
    a test that waits for an election waits milliseconds, not a third of a second.
    """
    return Settings(
        node_id=1,
        peers="1=127.0.0.1:9001",
        data_dir=tmp_path / "data",
        heartbeat_ms=10,
        election_min_ms=30,
        election_max_ms=60,
        snapshot_threshold=8,
    )


@pytest.fixture
async def client(single_node: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted node.

    Entering `lifespan_context` runs the real startup path — the state directory
    is opened, the node is built, the driver task is spawned, and everything is
    torn down on the way out — so a test can never pass against wiring that would
    fail in production.
    """
    app = create_app(single_node)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://node") as http_client:
            yield http_client
