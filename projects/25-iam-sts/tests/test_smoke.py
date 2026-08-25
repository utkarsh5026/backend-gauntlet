"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V6. They assert the plumbing
(both apps boot, the store works, unauthenticated callers are refused) and they
pin the scaffold's contract: the first signed request raises `NotImplementedError`
until V1 exists.

Note what the suite cannot reach yet, and why that is correct: authentication
runs **before** dispatch, so there is no way to exercise the management actions
without a working verifier. That is the front door working as designed. When you
implement V1, the last tests here are the first ones that should fail — delete
them then.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from iam_sts.config import Settings
from iam_sts.errors import EntityAlreadyExists, NoSuchEntity
from iam_sts.state import IdentityStore


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_authz_healthz(authz_client: httpx.AsyncClient) -> None:
    """The second listener is a real app, not a route on the first one."""
    response = await authz_client.get("/healthz")
    assert response.status_code == 200


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_authz_plane_has_its_own_metrics(authz_client: httpx.AsyncClient) -> None:
    """The boss fight's numbers come from this process, so it scrapes separately."""
    response = await authz_client.get("/metrics")
    assert response.status_code == 200


async def test_unsigned_request_is_refused(client: httpx.AsyncClient) -> None:
    """Every request to the API plane must be signed. No exceptions, no bypass."""
    response = await client.get("/?Action=GetCallerIdentity&Version=2011-06-15")
    assert response.status_code == 403
    assert response.headers["x-amzn-errortype"] == "MissingAuthenticationToken"


async def test_unsigned_post_is_refused(client: httpx.AsyncClient) -> None:
    """The POST form of the Query protocol is refused identically."""
    response = await client.post("/", data={"Action": "CreateUser", "UserName": "alice"})
    assert response.status_code == 403
    assert response.headers["x-amzn-errortype"] == "MissingAuthenticationToken"


async def test_unauthenticated_caller_learns_nothing(client: httpx.AsyncClient) -> None:
    """A bogus action and a real one are indistinguishable without credentials.

    Authentication runs before dispatch precisely so that an unauthenticated
    caller cannot enumerate what this service implements. If these two responses
    ever diverge, that ordering has been broken.
    """
    real = await client.get("/?Action=GetCallerIdentity")
    fake = await client.get("/?Action=DefinitelyNotAnAction")
    assert real.status_code == fake.status_code == 403
    assert real.headers["x-amzn-errortype"] == fake.headers["x-amzn-errortype"]
    assert real.json() == fake.json()


# --- the identity store: plumbing, so it works on the scaffold ---------------


def test_store_seeds_the_account_root(settings: Settings) -> None:
    """The chicken-and-egg: somebody has to exist before anyone can sign."""
    store = IdentityStore(settings)
    key = store.seed_bootstrap()
    assert key.access_key_id == settings.bootstrap_access_key_id
    assert store.get_user("root").name == "root"

    principal = store.principal_for_user(store.get_user("root"))
    assert principal.is_root
    assert str(principal.arn).endswith(":root")


def test_store_crud_and_conflicts(settings: Settings) -> None:
    store = IdentityStore(settings)
    store.seed_bootstrap()

    user = store.create_user("alice")
    assert str(user.arn(settings.aws_partition)).endswith("user/alice")
    assert store.get_user("alice") is user

    with pytest.raises(EntityAlreadyExists):
        store.create_user("alice")
    with pytest.raises(NoSuchEntity):
        store.get_user("bob")


def test_store_version_advances_on_every_mutation(settings: Settings) -> None:
    """V5's invalidation leans on this: a stale artifact must be *provably* stale."""
    store = IdentityStore(settings)
    before = store.version
    store.create_user("alice")
    assert store.version > before

    after_user = store.version
    store.create_access_key("alice", "AKIATESTTESTTESTTEST", "secret")
    assert store.version > after_user


def test_access_key_secret_does_not_leak_through_repr(settings: Settings) -> None:
    """The habit the SPEC grades with a test: secrets do not survive a `repr`.

    This is not the full criterion — that one greps a whole signed exchange — but
    it closes the accident that actually happens, which is an object landing in a
    log line or a traceback.
    """
    store = IdentityStore(settings)
    key = store.create_access_key(
        store.create_user("alice").name, "AKIATESTTESTTESTTEST", "super-secret-value"
    )
    assert "super-secret-value" not in repr(key)
    assert "super-secret-value" not in repr(settings)
    assert key.secret_access_key.get_secret_value() == "super-secret-value"


# --- the worklist, pinned ----------------------------------------------------


async def test_signed_request_is_still_a_todo(
    client: httpx.AsyncClient, signer: Callable[[httpx.Request], httpx.Request]
) -> None:
    """The scaffold's front door. Delete this once V1 verifies a signature.

    Note this signs with `botocore` — so the moment V1 lands, this exact request
    is the one that must verify unmodified.
    """
    request = client.build_request("GET", "/?Action=GetCallerIdentity&Version=2011-06-15")
    with pytest.raises(NotImplementedError):
        await client.send(signer(request))


async def test_authorize_is_still_a_todo(authz_client: httpx.AsyncClient) -> None:
    """The hot path — V2's ARN parsing raises before V5's cache is even reached."""
    with pytest.raises(NotImplementedError):
        await authz_client.post(
            "/2025-01-01/authorize",
            json={
                "principal_arn": "arn:aws:iam::000000000000:user/alice",
                "action": "dynamodb:GetItem",
                "resource_arn": "arn:aws:dynamodb:us-east-1:000000000000:table/orders",
            },
        )


async def test_simulate_is_still_a_todo(authz_client: httpx.AsyncClient) -> None:
    """Same answer, uncached — V6 owns it, and parity with the live path is the bar."""
    with pytest.raises(NotImplementedError):
        await authz_client.post(
            "/2025-01-01/simulate",
            json={
                "principal_arn": "arn:aws:iam::000000000000:user/alice",
                "action": "s3:GetObject",
                "resource_arn": "arn:aws:s3:::bucket/key",
            },
        )


async def test_authorize_rejects_a_malformed_body(authz_client: httpx.AsyncClient) -> None:
    """Validation happens before any vertical, so this works on the scaffold."""
    response = await authz_client.post("/2025-01-01/authorize", json={"action": "s3:GetObject"})
    assert response.status_code == 422
