"""The HTTP surface, driven over ASGI against real Postgres and Redis.

This is the layer where the SPEC's protocol and security items are observable:
the redirect's status code and headers, auth rejecting before the handler runs,
the rate limiter's 429, and the metrics a scraper would see.
"""

from __future__ import annotations

import httpx
import pytest

from url_shortener.id_gen import base62_decode
from url_shortener.routes import MAX_CUSTOM_SLUG_LEN

from .conftest import unique_slug

LONG_URL = "https://example.com/a/very/long/destination?utm=1"


async def _create(
    client: httpx.AsyncClient, auth: dict[str, str], **body: object
) -> httpx.Response:
    return await client.post("/api/links", json={"url": LONG_URL, **body}, headers=auth)


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders_the_graded_metrics(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """The three the SPEC grades must be present by name, so a dashboard built
    against them keeps working."""
    created = await _create(client, auth)
    await client.get(f"/{created.json()['slug']}")

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "url_shortener_redirects_total" in response.text
    assert "url_shortener_cache_lookups_total" in response.text
    assert "url_shortener_ingest_queue_depth" in response.text


async def test_every_response_carries_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers.get("x-request-id")


# --------------------------------------------------------------------------- #
# security: auth
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong-key"},
        {"authorization": "test-secret-key"},  # no scheme
        {"authorization": "bearer test-secret-key"},  # wrong case
        {"authorization": "Bearer  test-secret-key"},  # leading space in token
        {"authorization": "Bearer test-secret-key "},  # trailing space in token
        {"authorization": "Basic test-secret-key"},
    ],
)
async def test_write_path_rejects_bad_credentials(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.post("/api/links", json={"url": LONG_URL}, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_auth_runs_before_the_handler(client: httpx.AsyncClient) -> None:
    """An unauthenticated request with a *malformed body* must still be a 401,
    not a 422 - proof the handler never ran."""
    response = await client.post("/api/links", json={"nope": 1})
    assert response.status_code == 401


async def test_redirects_are_public(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    slug = (await _create(client, auth)).json()["slug"]
    response = await client.get(f"/{slug}")
    assert response.status_code == 302


async def test_the_key_never_appears_in_a_response(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/links", json={"url": LONG_URL}, headers={"authorization": "Bearer leak-me"}
    )
    assert "leak-me" not in response.text


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #


async def test_create_returns_a_slug_that_decodes_to_its_id(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await _create(client, auth)

    assert response.status_code == 201
    body = response.json()
    assert body["long_url"] == LONG_URL
    assert body["short_url"].endswith(f"/{body['slug']}")
    # The slug *is* the id - V1's whole premise, visible from the outside.
    assert base62_decode(body["slug"]) > 0


async def test_create_accepts_a_custom_slug(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    slug = unique_slug("vanity")
    response = await _create(client, auth, custom_slug=slug)
    assert response.status_code == 201
    assert response.json()["slug"] == slug


async def test_a_duplicate_slug_is_rejected(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """Uniqueness is the database's job, so this is the constraint violation
    being translated - not a check-then-insert race."""
    slug = unique_slug("dupe")
    assert (await _create(client, auth, custom_slug=slug)).status_code == 201

    response = await _create(client, auth, custom_slug=slug)
    assert response.status_code == 400
    assert response.json() == {"error": "slug already in use"}


@pytest.mark.parametrize(
    "slug",
    ["", "  ", "has space", "has/slash", "healthz", "API", "a" * (MAX_CUSTOM_SLUG_LEN + 1)],
)
async def test_bad_custom_slugs_are_rejected(
    client: httpx.AsyncClient, auth: dict[str, str], slug: str
) -> None:
    response = await _create(client, auth, custom_slug=slug)
    assert response.status_code == 400


@pytest.mark.parametrize(
    "url", ["http://example.com", "javascript:alert(1)", "https://127.0.0.1/", "not-a-url"]
)
async def test_bad_urls_are_rejected(
    client: httpx.AsyncClient, auth: dict[str, str], url: str
) -> None:
    response = await client.post("/api/links", json={"url": url}, headers=auth)
    assert response.status_code == 400


async def test_creating_a_link_clears_a_stale_negative_entry(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """Someone probed for the vanity name first, so a `Missing` was cached. The
    link must work immediately, not after the negative TTL lapses."""
    slug = unique_slug("probed")
    assert (await client.get(f"/{slug}")).status_code == 404  # caches the absence

    assert (await _create(client, auth, custom_slug=slug)).status_code == 201

    response = await client.get(f"/{slug}")
    assert response.status_code == 302


# --------------------------------------------------------------------------- #
# redirect
# --------------------------------------------------------------------------- #


async def test_redirect_is_302_and_uncacheable(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """302, not 301. A permanent redirect is cached by browsers and proxies, so
    every click after the first would never reach the server and go uncounted -
    and analytics is the product."""
    slug = (await _create(client, auth)).json()["slug"]

    response = await client.get(f"/{slug}")

    assert response.status_code == 302
    assert response.headers["location"] == LONG_URL
    assert response.headers["cache-control"] == "no-store"


async def test_the_first_redirect_misses_and_the_second_hits(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """The cache-aside cycle, visible from outside via `X-Cache`."""
    slug = (await _create(client, auth)).json()["slug"]

    first = await client.get(f"/{slug}")
    second = await client.get(f"/{slug}")

    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"


async def test_an_unknown_slug_is_a_json_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/{unique_slug('nope')}")
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


async def test_a_repeated_404_is_served_from_the_negative_cache(
    client: httpx.AsyncClient,
) -> None:
    """The negative-cache criterion, observed through the public inspector.

    A 404 response carries no `X-Cache` header - it is an error envelope, not a
    resolution - so the outcome is read from `/api/debug/resolve`, which reports
    the same `resolve_slug` result the redirect used.
    """
    slug = unique_slug("flood")
    assert (await client.get(f"/{slug}")).status_code == 404

    body = (await client.get(f"/api/debug/resolve/{slug}")).json()
    assert body["found"] is False
    assert body["cache"] == "negative", "the first 404 should have cached the absence"


# --------------------------------------------------------------------------- #
# the debug inspector
# --------------------------------------------------------------------------- #


async def test_debug_resolve_decodes_the_snowflake(
    client: httpx.AsyncClient, auth: dict[str, str], settings_node_id: int
) -> None:
    slug = (await _create(client, auth)).json()["slug"]

    body = (await client.get(f"/api/debug/resolve/{slug}")).json()

    assert body["found"] is True
    assert body["long_url"] == LONG_URL
    assert body["cache"] in {"hit", "miss"}
    assert body["snowflake"]["node_id"] == settings_node_id
    assert body["snowflake"]["timestamp_unix_ms"] > body["snowflake"]["custom_epoch_unix_ms"]


async def test_debug_resolve_is_public_and_records_no_click(
    client: httpx.AsyncClient,
) -> None:
    """A pure inspector: watching the dashboard must not skew the analytics it
    is displaying."""
    response = await client.get(f"/api/debug/resolve/{unique_slug('none')}")
    assert response.status_code == 200
    assert response.json()["found"] is False


# --------------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------------- #


async def test_the_write_path_is_rate_limited_per_key(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    statuses = [(await _create(client, auth)).status_code for _ in range(12)]

    assert statuses[:10] == [201] * 10
    assert statuses[10] == 429


async def test_a_429_keeps_the_json_envelope_and_a_retry_hint(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    responses = [await _create(client, auth) for _ in range(11)]
    response = responses[-1]

    assert response.status_code == 429
    assert response.json() == {"error": "too many requests"}
    assert int(response.headers["retry-after"]) >= 1
    assert response.headers["x-ratelimit-limit"] == "10"


async def test_redirects_are_never_rate_limited(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """The limiter is scoped to the write path - throttling the read path would
    throttle the product."""
    slug = (await _create(client, auth)).json()["slug"]
    statuses = {(await client.get(f"/{slug}")).status_code for _ in range(30)}
    assert statuses == {302}


# --------------------------------------------------------------------------- #
# the remaining worklist
# --------------------------------------------------------------------------- #


async def test_stats_is_still_unimplemented(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """The scaffold's worklist, pinned. Delete this when stats aggregation lands."""
    slug = (await _create(client, auth)).json()["slug"]
    with pytest.raises(NotImplementedError):
        await client.get(f"/api/links/{slug}/stats", headers=auth)
