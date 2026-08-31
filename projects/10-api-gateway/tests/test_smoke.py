"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V4 (V1's live in
`test_proxy_acceptance.py`). They assert the plumbing: the app boots, config
parses, the shipped example route table is valid, the gateway's own endpoints are
not swallowed by the catch-all, and the proxy paths raise until you build them.

The last group is the worklist made executable. When you implement a vertical,
those are the first things that should fail — delete them then.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from api_gateway.balancer import Backend, Balancer, LbPolicy
from api_gateway.config import GatewayConfig, Settings, parse_backends
from api_gateway.health import CircuitBreaker, CircuitState
from api_gateway.router import Router
from api_gateway.tls import server_context, upstream_context
from tests.conftest import drain

PROJECT_DIR = Path(__file__).resolve().parent.parent

# --- wiring -------------------------------------------------------------------


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    """Also the ordering regression test that matters most in this project: a
    catch-all registered before `/metrics` would proxy the scrape to a backend."""
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_admin_routes_lists_the_compiled_table(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/routes")
    assert response.status_code == 200
    assert response.json() == {"routes": ["default"]}


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    """An inbound id survives the hop — it is what correlates a client's failed
    request with the matched route and chosen backend in the gateway's log."""
    response = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


# --- config -------------------------------------------------------------------


def test_backends_parse_into_a_pool() -> None:
    assert parse_backends("127.0.0.1:9010, 127.0.0.1:9011") == [
        "127.0.0.1:9010",
        "127.0.0.1:9011",
    ]


@pytest.mark.parametrize("raw", ["", "   ", ","])
def test_empty_backend_pool_fails_at_startup(raw: str) -> None:
    """A route with no backends can only ever 503. Failing the process beats
    finding out at the first request with no explanation."""
    with pytest.raises(ValidationError):
        Settings(upstream_backends=raw)


def test_millisecond_env_becomes_seconds() -> None:
    """The environment speaks ms because that is readable; asyncio and httpx speak
    seconds. The conversion happens once, here."""
    settings = Settings(request_timeout_ms=2_500, upstream_connect_timeout_ms=750)
    assert settings.request_timeout == 2.5
    assert settings.connect_timeout == 0.75


def test_tls_needs_both_halves_of_the_keypair() -> None:
    """A cert with no key is a typo, and silently serving plain HTTP because of one
    is how a 'TLS-terminating' gateway ends up not terminating TLS."""
    assert not Settings(tls_cert="/tmp/a.crt").tls_enabled
    assert Settings(tls_cert="/tmp/a.crt", tls_key="/tmp/a.key").tls_enabled


def test_shipped_example_config_is_valid() -> None:
    """`gateway.example.json` is documentation that can rot. This keeps it honest —
    and it is also the fixture the V2 tests will want, since it has the overlapping
    `/api` and `/api/v2/users` prefixes longest-match precedence is about."""
    cfg = GatewayConfig.load(PROJECT_DIR / "gateway.example.json")
    names = [r.name for r in cfg.routes]
    assert names == ["users-api", "api-fallback", "default"]
    assert cfg.routes[0].upstream.lb is LbPolicy.P2C
    assert cfg.routes[0].host == "api.example.com"


def test_configured_methods_are_normalized_to_uppercase() -> None:
    """So a config that says `get` behaves like one that says `GET`, rather than
    matching nothing and looking like a routing bug."""
    cfg = GatewayConfig.model_validate(
        {
            "routes": [
                {
                    "name": "r",
                    "path_prefix": "/",
                    "methods": ["get", " post "],
                    "upstream": {"backends": ["127.0.0.1:1"]},
                }
            ]
        }
    )
    assert cfg.routes[0].methods == ["GET", "POST"]


def test_demo_config_is_a_catch_all_over_the_pool() -> None:
    """The zero-files path: `make run` has to work before you have written a config."""
    cfg = Settings(upstream_backends="a:1,b:2", lb_policy=LbPolicy.P2C).gateway_config()
    assert len(cfg.routes) == 1
    assert cfg.routes[0].path_prefix == "/"
    assert cfg.routes[0].upstream.backends == ["a:1", "b:2"]
    assert cfg.routes[0].upstream.lb is LbPolicy.P2C


# --- the compiled table -------------------------------------------------------


def test_build_compiles_routes_pools_and_breakers() -> None:
    """`Router.build` is plumbing and is expected to work now: every route gets a
    balancer, every backend gets its own breaker carrying the operator's numbers."""
    cfg = GatewayConfig.load(PROJECT_DIR / "gateway.example.json")
    router = Router.build(cfg, failure_threshold=7, open_cooldown=1.5)

    assert router.route_names() == ["users-api", "api-fallback", "default"]
    assert len(router.backends()) == 6
    assert all(b.circuit.failure_threshold == 7 for b in router.backends())
    assert all(b.circuit.open_cooldown == 1.5 for b in router.backends())


def test_a_fresh_backend_starts_idle_and_closed() -> None:
    """The signals the balancer reads start neutral, and — importantly — they do
    not update themselves. Until the proxy path does the accounting, least-conn
    and P2C are reading zeros and behaving exactly like round-robin."""
    backend = Backend("127.0.0.1:9010")
    assert backend.in_flight == 0
    assert backend.ewma_seconds == 0.0
    assert backend.circuit.state is CircuitState.CLOSED


# --- the scaffold's worklist, pinned -----------------------------------------


async def test_proxying_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """Any path that is not one of the gateway's own is a proxy target. Delete once
    V1/V2/V3 land."""
    with pytest.raises(NotImplementedError):
        await client.get("/anything")


def test_route_matching_is_still_a_todo() -> None:
    """Delete once V2 lands."""
    router = Router.build(GatewayConfig.demo(["127.0.0.1:9010"]))
    with pytest.raises(NotImplementedError):
        router.match_request("gateway.test", "/", "GET")


def test_backend_selection_is_still_a_todo() -> None:
    """Delete once V3 lands."""
    balancer = Balancer(LbPolicy.ROUND_ROBIN, [Backend("127.0.0.1:9010")])
    with pytest.raises(NotImplementedError):
        balancer.pick()


def test_the_circuit_breaker_is_still_a_todo() -> None:
    """Delete once V4 lands. Note that `state` already answers — a metrics scrape
    must be able to read it without nudging the machine."""
    breaker = CircuitBreaker()
    assert breaker.state is CircuitState.CLOSED
    with pytest.raises(NotImplementedError):
        breaker.allow()


def test_mtls_contexts_are_still_a_todo() -> None:
    """Delete once the mTLS security item lands."""
    with pytest.raises(NotImplementedError):
        server_context("/tmp/a.crt", "/tmp/a.key")
    with pytest.raises(NotImplementedError):
        upstream_context()


# --- the acceptance harness itself, guarded ---------------------------------


async def test_drain_actually_streams_a_streaming_response() -> None:
    """A guard on `drain`, not on your code.

    If this breaks, every streaming assertion above becomes a lie that fails
    against a correct `forward`. It pins the two things `drain` gets right: the
    chunks come through in order, and the `background` task — where the upstream
    response is closed and its connection released — actually runs.
    """
    closed = False

    async def chunks() -> AsyncIterator[bytes]:
        yield b"a"
        await asyncio.sleep(0.05)
        yield b"b"

    async def close() -> None:
        nonlocal closed
        closed = True

    response = StreamingResponse(
        chunks(),
        status_code=207,
        headers={"x-note": "streamed"},
        background=BackgroundTask(close),
    )
    status, headers, body = await drain(response)

    assert status == 207
    assert headers["x-note"] == "streamed"
    assert body == b"ab"
    assert closed, "drain must run the background task — that is where the upstream closes"
