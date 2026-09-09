"""Dual-path object auth: **presigned URLs** or **access credentials**.

An open `PUT /{bucket}/{key}` is an open disk for the whole internet. When a
secret is configured, object routes are gated and a request is accepted if
**either** path succeeds:

1. **Presigned URL** — the query carries `expires` and `signature`, an HMAC over
   the method, the object path and the expiry. Self-describing like a JWT: the
   server reconstructs the claims from the request itself and checks the MAC, so
   no state is stored per URL and no lookup happens on the hot path. Mint one
   with `POST /presign`.
2. **Access credentials** — `Authorization: Bearer <ACCESS_KEY_ID>:<SECRET>`
   (or the secret alone). Lets the ordinary `PUT /{bucket}/{key}` path work with
   no query juggling, for a client that legitimately holds the long-lived key.

This is a simplified learning shape, not AWS SigV4 — SigV4 signs headers, the
payload hash and a scoped derived key, which is project 25's problem. The
*brain* is the same HMAC, and session-scoped tokens would layer on here.

## What is deliberately careful

**Constant-time comparison.** A byte-by-byte `==` on a MAC leaks where the first
mismatch is via timing, and a few thousand requests turn that into the whole
signature. `hmac.compare_digest` is the standard answer.

**Partial query means deny.** If either `expires` or `signature` is present, the
request is judged as presigned and never falls through to the bearer path.
Otherwise stripping the signature from a signed URL would silently downgrade it
to whatever the header happens to allow.

**Expiry is checked before the MAC.** An expired-but-valid signature and a
forged one both come back as the same opaque 403, so probing cannot distinguish
them.

Never log a secret or a write-granting signed URL — the URL *is* the credential.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import AccessDenied, InvalidRequest
from .objects import utc_now

if TYPE_CHECKING:
    from .config import Settings

__all__ = [
    "AuthConfig",
    "ObjectAuthMiddleware",
    "PresignRequest",
    "QUERY_EXPIRES",
    "QUERY_SIGNATURE",
    "authorize_object",
    "credentials_match",
    "sign",
    "verify",
]

QUERY_EXPIRES = "expires"
"""Query parameter carrying the unix-seconds expiry of a presigned URL."""

QUERY_SIGNATURE = "signature"
"""Query parameter carrying the hex-encoded HMAC."""


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Long-lived credentials. Never log `secret_access_key`."""

    access_key_id: str
    secret_access_key: str

    @classmethod
    def from_settings(cls, settings: Settings) -> AuthConfig | None:
        """Build from `Settings`, or `None` when no secret is configured.

        Returning `None` rather than raising is what keeps tests and local
        development ungated without a special "auth off" flag: an unset secret
        *is* the off switch, and it is impossible to half-enable.
        """
        if not settings.secret_access_key:
            return None
        return cls(settings.access_key_id or "local", settings.secret_access_key)

    @property
    def credential_token(self) -> str:
        """`ACCESS_KEY_ID:SECRET` — the preferred bearer token."""
        return f"{self.access_key_id}:{self.secret_access_key}"


@dataclass(frozen=True, slots=True)
class PresignRequest:
    """What a client wants a presigned URL to authorise — the signed claims."""

    method: str
    bucket: str
    key: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PresignedUrl:
    """A path + query the holder may use until it expires."""

    path_and_query: str
    expires: int


def canonical_string(method: str, bucket: str, key: str, expires: int) -> str:
    """The exact bytes both `sign` and `verify` HMAC.

    Newline-separated and fixed-order so the signature binds *all four* claims.
    Sign only the key and a URL for `DELETE` also works for `GET`; sign without
    the expiry and the URL is eternal. Every field in the canonical string is a
    field an attacker cannot change.
    """
    return f"{method.upper()}\n/{bucket}/{key}\n{expires}"


def _mac(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def sign(config: AuthConfig, request: PresignRequest) -> PresignedUrl:
    """Mint a presigned URL. The expiry must be strictly in the future."""
    expires = int(request.expires_at.timestamp())
    if expires <= int(utc_now().timestamp()):
        raise InvalidRequest("presign expiry must be in the future")

    signature = _mac(
        config.secret_access_key,
        canonical_string(request.method, request.bucket, request.key, expires),
    )
    # The key is percent-encoded with `/` kept: it is a path segment sequence in
    # the URL even though it is one flat key to storage, and a raw `?` or `#`
    # inside it would otherwise truncate the query we are about to append.
    path = f"/{request.bucket}/{quote(request.key, safe='/')}"
    return PresignedUrl(
        path_and_query=(f"{path}?{QUERY_EXPIRES}={expires}&{QUERY_SIGNATURE}={signature}"),
        expires=expires,
    )


def verify(
    config: AuthConfig,
    method: str,
    bucket: str,
    key: str,
    expires: int,
    signature: str,
    now: datetime | None = None,
) -> None:
    """Accept or reject a presigned object request, or raise `AccessDenied`."""
    moment = now or utc_now()
    if int(moment.timestamp()) >= expires:
        raise AccessDenied()

    expected = _mac(config.secret_access_key, canonical_string(method, bucket, key, expires))
    if not hmac.compare_digest(expected, signature):
        raise AccessDenied()


def credentials_match(config: AuthConfig, authorization: str | None) -> bool:
    """Whether `Authorization` presents valid long-lived credentials.

    Accepts `Bearer <ACCESS_KEY_ID>:<SECRET>` and the API-key-style
    `Bearer <SECRET>` alone.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        return False
    token = authorization[len("Bearer ") :]

    if ":" in token:
        key_id, _, secret = token.partition(":")
        return hmac.compare_digest(key_id, config.access_key_id) and hmac.compare_digest(
            secret, config.secret_access_key
        )
    return hmac.compare_digest(token, config.secret_access_key)


def authorize_object(
    config: AuthConfig,
    *,
    method: str,
    bucket: str,
    key: str,
    expires: int | None,
    signature: str | None,
    authorization: str | None,
    now: datetime | None = None,
) -> None:
    """Authorise one object request: presign if the query is there, else bearer.

    Raises `AccessDenied` when neither path succeeds. See the module docstring
    on why a *partial* presign query denies rather than falling through.
    """
    if expires is not None or signature is not None:
        if expires is None or not signature:
            raise AccessDenied()
        verify(config, method, bucket, key, expires, signature, now)
        return

    if credentials_match(config, authorization):
        return
    raise AccessDenied()


def expires_in(seconds: int) -> datetime:
    """`now + seconds`, for minting. Rejects a non-positive window."""
    if seconds <= 0:
        raise InvalidRequest("expires_in_secs must be greater than zero")
    return utc_now() + timedelta(seconds=seconds)


class ObjectAuthMiddleware:
    """Gates object routes when a secret is configured.

    Raw ASGI rather than a Starlette `BaseHTTPMiddleware`, for the same reason
    `common_telemetry` is: `BaseHTTPMiddleware` wraps each request in an anyio
    task pair, and on a path that streams multi-gigabyte bodies that indirection
    is a real cost rather than a stylistic one.

    ## Which paths it covers

    Only `/{bucket}/{key}` — two or more path segments. `/healthz` stays open
    (a liveness probe that needs credentials is a liveness probe that reports
    your auth config), and so do `PUT /{bucket}` and `GET /{bucket}`, matching
    the Rust router's `route_layer` placement. `/presign` does its own,
    stricter check: it requires the long-lived credentials specifically, because
    minting a URL is delegating access.

    With no `AuthConfig` this is a no-op, which is why an unset
    `SECRET_ACCESS_KEY` means an open store rather than a broken one.
    """

    __slots__ = ("app", "config")

    def __init__(self, app: ASGIApp, config: AuthConfig | None) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.config is None or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", "")).lstrip("/")
        bucket, separator, key = path.partition("/")
        if not separator or not bucket or not key or bucket == "presign":
            await self.app(scope, receive, send)
            return

        from urllib.parse import parse_qs, unquote

        raw_query: bytes = scope.get("query_string", b"")
        query = parse_qs(raw_query.decode("latin-1"))
        raw_expires = query.get(QUERY_EXPIRES, [None])[0]
        signature = query.get(QUERY_SIGNATURE, [None])[0] or None

        expires: int | None = None
        if raw_expires is not None:
            try:
                expires = int(raw_expires)
            except ValueError:
                # A malformed expiry is a presign attempt that cannot be
                # verified, not an invitation to fall through to the bearer
                # path — see the module docstring.
                await _deny(send)
                return

        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1") for name, value in raw_headers
        }

        try:
            authorize_object(
                self.config,
                method=str(scope.get("method", "GET")),
                bucket=bucket,
                key=unquote(key),
                expires=expires,
                signature=signature,
                authorization=headers.get("authorization"),
            )
        except AccessDenied:
            await _deny(send)
            return

        await self.app(scope, receive, send)


async def _deny(send: Send) -> None:
    """Emit the S3 `AccessDenied` envelope without going through the app.

    Written here rather than raised, because an exception from inside raw ASGI
    middleware never reaches FastAPI's handler registry — it would surface as an
    unhandled 500 and leak a traceback instead of the 403 the client needs.
    """
    from .errors import AccessDenied as _AccessDenied
    from .errors import error_xml

    body = error_xml(_AccessDenied.code, _AccessDenied.message).encode("utf-8")
    start: Message = {
        "type": "http.response.start",
        "status": 403,
        "headers": [
            (b"content-type", b"application/xml"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    }
    await send(start)
    await send({"type": "http.response.body", "body": body})
