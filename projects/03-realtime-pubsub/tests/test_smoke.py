"""Scaffold smoke tests — proof the wiring is sound.

These are deliberately not vertical acceptance tests (those live in
`test_hub.py`, `test_backpressure.py`, `test_presence.py`). They assert the
plumbing: the app boots, the routes exist, metrics render, auth fails closed,
the admin API degrades to 503 without a DB, and the end-to-end WebSocket path
works. They also pin the scaffold's contract — the cluster bridge raises until
you build V4. When you implement it, `test_cluster_publish_is_still_a_todo` is
the first thing that should fail; delete it then.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import TEST_TOKEN
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from realtime_pubsub.cluster import ClusterBridge
from realtime_pubsub.config import Settings
from realtime_pubsub.hub import Hub
from realtime_pubsub.main import create_app


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


async def test_admin_is_503_without_a_database(client: httpx.AsyncClient) -> None:
    """The pub/sub core is store-free: no DATABASE_URL disables `/admin` and
    leaves everything else serving."""
    response = await client.get("/admin/people")
    assert response.status_code == 503


async def test_debug_health_reports_disabled_and_unused_bus(client: httpx.AsyncClient) -> None:
    response = await client.get("/debug/health")
    assert response.status_code == 200
    body = response.json()
    assert body["db"]["state"] == "disabled"
    assert body["cluster_mode"] is False
    assert body["ws_auth_configured"] is True
    # Never the secret itself — only whether one is configured.
    assert TEST_TOKEN not in response.text


# --- the WebSocket path -------------------------------------------------------
#
# These use Starlette's `TestClient`, not the `httpx` fixture: httpx has no
# WebSocket client, so this is the only way to drive `/ws` in-process. It runs
# the app in a background thread with its own event loop, which is why each test
# builds its own client rather than sharing the async fixture.


@pytest.fixture
def ws_client(settings: Settings):  # noqa: ANN201 - TestClient is a context manager
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def test_upgrade_without_a_token_is_refused(ws_client: TestClient) -> None:
    """Authenticate before accepting — an anonymous client never gets a socket."""
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect("/ws") as socket:
            socket.receive_text()


def test_upgrade_with_a_wrong_token_is_refused(ws_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect("/ws?token=nope") as socket:
            socket.receive_text()


def test_subscribe_then_publish_round_trips(ws_client: TestClient) -> None:
    """The whole V1+V3 path over a real socket: subscribe gets a presence frame,
    a publish comes back as a message frame."""
    url = f"/ws?token={TEST_TOKEN}&identity=alice"
    with ws_client.websocket_connect(url) as socket:
        socket.send_json({"type": "subscribe", "topic": "room1"})
        presence = socket.receive_json()
        assert presence == {"type": "presence", "topic": "room1", "members": ["alice"]}

        socket.send_json({"type": "publish", "topic": "room1", "payload": {"hello": "world"}})
        message = socket.receive_json()
        assert message == {
            "type": "message",
            "topic": "room1",
            "payload": {"hello": "world"},
        }


def test_two_sockets_see_each_other_in_presence(ws_client: TestClient) -> None:
    url = f"/ws?token={TEST_TOKEN}&identity="
    with ws_client.websocket_connect(url + "alice") as alice:
        alice.send_json({"type": "subscribe", "topic": "room1"})
        assert alice.receive_json()["members"] == ["alice"]

        with ws_client.websocket_connect(url + "bob") as bob:
            bob.send_json({"type": "subscribe", "topic": "room1"})
            # Both sockets are told the new roster — presence is broadcast to
            # the room, not just to the joiner.
            assert sorted(alice.receive_json()["members"]) == ["alice", "bob"]
            assert sorted(bob.receive_json()["members"]) == ["alice", "bob"]


def test_a_malformed_frame_gets_an_error_not_a_close(ws_client: TestClient) -> None:
    """SPEC protocol checklist: reject a bad frame with an `error` message,
    don't drop the connection silently."""
    with ws_client.websocket_connect(f"/ws?token={TEST_TOKEN}") as socket:
        socket.send_text("{not json at all")
        error = socket.receive_json()
        assert error["type"] == "error"
        assert "invalid message" in error["reason"]

        # The socket is still usable afterwards.
        socket.send_json({"type": "subscribe", "topic": "room1"})
        assert socket.receive_json()["type"] == "presence"


def test_an_unknown_message_type_gets_an_error(ws_client: TestClient) -> None:
    with ws_client.websocket_connect(f"/ws?token={TEST_TOKEN}") as socket:
        socket.send_json({"type": "definitely-not-a-command"})
        assert socket.receive_json()["type"] == "error"


def test_a_disconnected_socket_leaves_the_room(ws_client: TestClient) -> None:
    """Every teardown path removes the connection from hub *and* presence — the
    anti-ghost criterion, over a real socket."""
    url = f"/ws?token={TEST_TOKEN}&identity="
    with ws_client.websocket_connect(url + "alice") as alice:
        alice.send_json({"type": "subscribe", "topic": "room1"})
        alice.receive_json()

        with ws_client.websocket_connect(url + "bob") as bob:
            bob.send_json({"type": "subscribe", "topic": "room1"})
            alice.receive_json()
            bob.receive_json()

        # Bob's socket closed here. Alice re-subscribing forces a fresh snapshot;
        # bob must be gone from it.
        alice.send_json({"type": "subscribe", "topic": "room1"})
        assert alice.receive_json()["members"] == ["alice"]


# --- the V4 worklist, pinned --------------------------------------------------


async def test_cluster_publish_is_still_a_todo() -> None:
    """Delete this once the bus lands."""
    bridge = ClusterBridge("redis://127.0.0.1:1/0", "node-a", Hub())
    try:
        with pytest.raises(NotImplementedError):
            await bridge.publish("room1", {"hello": "world"})
    finally:
        await bridge.aclose()


async def test_cluster_run_is_still_a_todo() -> None:
    """Delete this once the receive side lands."""
    bridge = ClusterBridge("redis://127.0.0.1:1/0", "node-a", Hub())
    try:
        with pytest.raises(NotImplementedError):
            await bridge.run()
    finally:
        await bridge.aclose()
