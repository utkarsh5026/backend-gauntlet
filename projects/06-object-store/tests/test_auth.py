"""Auth: presigned URLs, bearer credentials, and what the gate covers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from object_store.auth import (
    AuthConfig,
    PresignRequest,
    authorize_object,
    canonical_string,
    credentials_match,
    sign,
    verify,
)
from object_store.config import Settings
from object_store.errors import AccessDenied, InvalidRequest
from object_store.main import build_state, create_app
from object_store.objects import utc_now

CONFIG = AuthConfig(access_key_id="local", secret_access_key="s3cret")


@pytest.fixture
def secured_settings(data_dir: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=data_dir,
        secret_access_key="s3cret",
        access_key_id="local",
        index_url="",
    )


@pytest_asyncio.fixture
async def secured(secured_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(secured_settings)
    app.state.app_state = build_state(secured_settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://store") as http:
        yield http
    app.state.app_state.store.close()


# ── signing ─────────────────────────────────────────────────────────────────


def test_the_canonical_string_binds_every_claim() -> None:
    """Change any one of the four and the signature must change with it."""
    base = canonical_string("GET", "photos", "a.jpg", 1_700_000_000)

    assert base != canonical_string("PUT", "photos", "a.jpg", 1_700_000_000)
    assert base != canonical_string("GET", "other", "a.jpg", 1_700_000_000)
    assert base != canonical_string("GET", "photos", "b.jpg", 1_700_000_000)
    assert base != canonical_string("GET", "photos", "a.jpg", 1_700_000_001)


def test_a_freshly_signed_url_verifies() -> None:
    expires_at = utc_now() + timedelta(minutes=5)
    signed = sign(CONFIG, PresignRequest("GET", "photos", "a.jpg", expires_at))

    signature = signed.path_and_query.split("signature=")[1]
    verify(CONFIG, "GET", "photos", "a.jpg", signed.expires, signature)


def test_signing_with_a_past_expiry_is_rejected() -> None:
    with pytest.raises(InvalidRequest):
        sign(
            CONFIG,
            PresignRequest("GET", "photos", "a.jpg", utc_now() - timedelta(seconds=1)),
        )


def test_an_expired_signature_is_denied() -> None:
    expires_at = utc_now() + timedelta(seconds=30)
    signed = sign(CONFIG, PresignRequest("GET", "photos", "a.jpg", expires_at))
    signature = signed.path_and_query.split("signature=")[1]

    with pytest.raises(AccessDenied):
        verify(
            CONFIG,
            "GET",
            "photos",
            "a.jpg",
            signed.expires,
            signature,
            now=utc_now() + timedelta(minutes=1),
        )


def test_a_url_signed_for_one_method_does_not_work_for_another() -> None:
    """A read URL must not become a write URL by changing the verb."""
    signed = sign(
        CONFIG,
        PresignRequest("GET", "photos", "a.jpg", utc_now() + timedelta(minutes=5)),
    )
    signature = signed.path_and_query.split("signature=")[1]

    with pytest.raises(AccessDenied):
        verify(CONFIG, "PUT", "photos", "a.jpg", signed.expires, signature)


def test_a_url_signed_for_one_key_does_not_work_for_another() -> None:
    signed = sign(
        CONFIG,
        PresignRequest("GET", "photos", "a.jpg", utc_now() + timedelta(minutes=5)),
    )
    signature = signed.path_and_query.split("signature=")[1]

    with pytest.raises(AccessDenied):
        verify(CONFIG, "GET", "photos", "secret.jpg", signed.expires, signature)


def test_a_forged_signature_is_denied() -> None:
    with pytest.raises(AccessDenied):
        verify(CONFIG, "GET", "photos", "a.jpg", 4_000_000_000, "0" * 64)


def test_extending_the_expiry_invalidates_the_signature() -> None:
    """The expiry is signed, so it cannot be edited in the URL."""
    signed = sign(
        CONFIG,
        PresignRequest("GET", "photos", "a.jpg", utc_now() + timedelta(minutes=1)),
    )
    signature = signed.path_and_query.split("signature=")[1]

    with pytest.raises(AccessDenied):
        verify(CONFIG, "GET", "photos", "a.jpg", signed.expires + 86400, signature)


# ── bearer credentials ──────────────────────────────────────────────────────


def test_both_bearer_forms_are_accepted() -> None:
    assert credentials_match(CONFIG, "Bearer local:s3cret")
    assert credentials_match(CONFIG, "Bearer s3cret")


def test_wrong_or_missing_credentials_are_rejected() -> None:
    assert not credentials_match(CONFIG, "Bearer local:wrong")
    assert not credentials_match(CONFIG, "Bearer wrong:s3cret")
    assert not credentials_match(CONFIG, "Basic bG9jYWw6czNjcmV0")
    assert not credentials_match(CONFIG, None)
    assert not credentials_match(CONFIG, "")


# ── the dispatch rule ───────────────────────────────────────────────────────


def test_a_partial_presign_query_denies_rather_than_falling_through() -> None:
    """Stripping the signature must not downgrade a signed URL to bearer auth."""
    with pytest.raises(AccessDenied):
        authorize_object(
            CONFIG,
            method="GET",
            bucket="photos",
            key="a.jpg",
            expires=4_000_000_000,
            signature=None,
            authorization="Bearer s3cret",
        )


def test_bearer_is_used_when_there_is_no_presign_query() -> None:
    authorize_object(
        CONFIG,
        method="PUT",
        bucket="photos",
        key="a.jpg",
        expires=None,
        signature=None,
        authorization="Bearer local:s3cret",
    )


# ── the gate, over HTTP ─────────────────────────────────────────────────────


async def test_object_routes_are_gated_when_a_secret_is_set(
    secured: AsyncClient,
) -> None:
    await secured.put("/photos", headers={"authorization": "Bearer s3cret"})

    denied = await secured.put("/photos/a.txt", content=b"nope")
    assert denied.status_code == 403

    allowed = await secured.put(
        "/photos/a.txt",
        content=b"yes",
        headers={"authorization": "Bearer local:s3cret"},
    )
    assert allowed.status_code == 200


async def test_healthz_stays_open(secured: AsyncClient) -> None:
    """A liveness probe that needs credentials reports your auth config."""
    assert (await secured.get("/healthz")).status_code == 200


async def test_a_presigned_url_works_without_a_header(
    secured: AsyncClient,
) -> None:
    await secured.put("/photos", headers={"authorization": "Bearer s3cret"})
    await secured.put(
        "/photos/signed.txt",
        content=b"signed content",
        headers={"authorization": "Bearer s3cret"},
    )

    minted = await secured.post(
        "/presign",
        json={
            "method": "GET",
            "bucket": "photos",
            "key": "signed.txt",
            "expires_in_secs": 300,
        },
        headers={"authorization": "Bearer s3cret"},
    )
    assert minted.status_code == 200

    got = await secured.get(minted.json()["url"])
    assert got.status_code == 200
    assert got.content == b"signed content"


async def test_minting_requires_the_long_lived_credentials(
    secured: AsyncClient,
) -> None:
    """Minting delegates access, so it cannot itself be delegated."""
    response = await secured.post(
        "/presign",
        json={
            "method": "PUT",
            "bucket": "photos",
            "key": "a.txt",
            "expires_in_secs": 300,
        },
    )
    assert response.status_code == 403


async def test_a_tampered_signature_is_denied_over_http(
    secured: AsyncClient,
) -> None:
    await secured.put("/photos", headers={"authorization": "Bearer s3cret"})
    minted = await secured.post(
        "/presign",
        json={
            "method": "GET",
            "bucket": "photos",
            "key": "a.txt",
            "expires_in_secs": 300,
        },
        headers={"authorization": "Bearer s3cret"},
    )
    url = minted.json()["url"]

    tampered = url.replace("signature=", "signature=00")
    assert (await secured.get(tampered)).status_code == 403


async def test_an_unparseable_expiry_is_denied(secured: AsyncClient) -> None:
    await secured.put("/photos", headers={"authorization": "Bearer s3cret"})
    response = await secured.get("/photos/a.txt?expires=not-a-number&signature=abc")
    assert response.status_code == 403


async def test_the_store_is_open_when_no_secret_is_configured(
    client: AsyncClient,
) -> None:
    """An unset secret is the off switch — there is no half-enabled state."""
    await client.put("/photos")
    assert (await client.put("/photos/open.txt", content=b"x")).status_code == 200
