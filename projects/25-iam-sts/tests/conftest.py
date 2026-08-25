"""Shared fixtures.

The acceptance tests for V1-V6 are yours to write (see the SPEC's "Proof" lines).
What lives here is only the harness: a node with predictable credentials, and
clients that speak to **both** of its listeners over ASGI.

Both apps share one `AppState`, exactly as they do in production — so a test can
write a policy on the API client and see the decision change on the authz client,
which is the loop the propagation-window criterion (V5) is measured over.

`signer` is the fixture V1 is graded against. It signs with `botocore` — the real
SDK's real signer — because a verifier tested against a signer you also wrote
proves only that you agree with yourself.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from iam_sts.config import Settings
from iam_sts.main import create_stack

TEST_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
TEST_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A node with its own audit path, so tests never share on-disk state."""
    return Settings(
        port=9025,
        authz_port=9026,
        audit_log_path=tmp_path / "audit.log",
        bootstrap_access_key_id=TEST_ACCESS_KEY_ID,
        bootstrap_secret_access_key=SecretStr(TEST_SECRET_ACCESS_KEY),
        # Small enough that an eviction test can fill it deliberately.
        decision_cache_size=64,
        # Short enough that a propagation test does not take a coffee break, and
        # long enough that a cache-hit test can still get a hit.
        decision_cache_ttl_seconds=0.5,
        audit_queue_size=32,
        # Tight, so a chaining test hits the cap without constructing five roles.
        max_role_chain_depth=2,
    )


@pytest.fixture
async def stack(settings: Settings) -> AsyncGenerator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    """A booted node: `(api, authz)` clients over one shared state.

    Entering `lifespan_context` runs the real startup path, so a test can never
    pass against wiring that would fail in production.
    """
    app, authz_app = create_stack(settings)
    async with app.router.lifespan_context(app):
        api_transport = httpx.ASGITransport(app=app)
        authz_transport = httpx.ASGITransport(app=authz_app)
        async with (
            httpx.AsyncClient(transport=api_transport, base_url="http://iam") as api,
            httpx.AsyncClient(transport=authz_transport, base_url="http://authz") as authz,
        ):
            yield api, authz


@pytest.fixture
async def client(stack: tuple[httpx.AsyncClient, httpx.AsyncClient]) -> httpx.AsyncClient:
    """The AWS-shaped API alone — what a signed caller talks to."""
    return stack[0]


@pytest.fixture
async def authz_client(stack: tuple[httpx.AsyncClient, httpx.AsyncClient]) -> httpx.AsyncClient:
    """The authorization endpoint alone — what projects 23/24/06 call."""
    return stack[1]


@pytest.fixture
def signer(settings: Settings) -> Callable[[httpx.Request], httpx.Request]:
    """Sign an `httpx.Request` with SigV4, using botocore's own signer.

    This is the fixture V1 is graded against, and the reason it uses botocore
    rather than a hand-rolled signer is the SPEC's first criterion: "a request
    signed by a **real** AWS SDK verifies unmodified". Any mismatch between this
    and your verifier is a bug in the verifier — botocore is the reference, and
    the canonical-request diff is the debugging technique.
    """
    # Imported lazily, and each ignored for the same reason: botocore ships no
    # type stubs. It is a dev-only dependency that exists solely to be the
    # reference signer V1 is graded against, so the untyped surface stops here
    # rather than leaking into the service.
    from botocore.auth import SigV4Auth  # pyright: ignore[reportMissingTypeStubs]
    from botocore.awsrequest import AWSRequest  # pyright: ignore[reportMissingTypeStubs]
    from botocore.credentials import Credentials  # pyright: ignore[reportMissingTypeStubs]

    credentials = Credentials(
        access_key=settings.bootstrap_access_key_id,
        secret_key=settings.bootstrap_secret_access_key.get_secret_value(),
    )

    def sign(request: httpx.Request, service: str = "sts") -> httpx.Request:
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={"host": request.url.host},
        )
        # pyright: ignore — untyped botocore surface, see the import comment above.
        SigV4Auth(credentials, service, settings.aws_region).add_auth(  # pyright: ignore[reportUnknownMemberType]
            aws_request
        )
        for key, value in aws_request.headers.items():
            request.headers[key] = value
        return request

    return sign
