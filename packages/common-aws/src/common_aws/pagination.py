"""Opaque continuation tokens — `NextToken`, `LastEvaluatedKey`, `Marker`.

Every listing API in the tier hands the client something to come back with:
DynamoDB's `LastEvaluatedKey`, SQS's `NextToken`, S3's `ContinuationToken`. They
are all the same primitive with different names, and the interesting property is
that they are **opaque**: the client is told to send it back and nothing else.

Opaque is a security boundary, not a style choice. A cursor that is a readable
JSON blob is an input the client can edit, and a client that edits it is asking
your service to resume a scan at a position it never issued — reading past the
end of a page, into another tenant's partition, or into a key range the caller
was never authorized for. So the cursor here is signed: tampering is detected
before the payload is ever looked at, and an expired cursor is refused rather
than resumed against a table that has moved on underneath it.

What goes *inside* the cursor is the service's decision and the interesting part:
a key to resume after, a snapshot id, a shard position. Deciding that — so a
paged `Query` returns every item exactly once even while writes land — is project
23's problem, not this module's. This owns the envelope: encode, sign, expire.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from typing import Any, cast

from .errors import InvalidParameterValue

__all__ = ["CursorCodec"]


class CursorCodec:
    """Signs and verifies pagination cursors for one service.

    The key is the service's own and never leaves it, so a cursor minted by one
    node verifies on another only if they share it — which is the deployment
    question worth being deliberate about: a per-process random key means a
    client's second page fails after a restart or behind a load balancer.
    """

    def __init__(self, secret: bytes, *, ttl_seconds: int | None = 3600) -> None:
        if not secret:
            raise ValueError("a cursor codec needs a non-empty secret")
        self._secret = secret
        self._ttl = ttl_seconds

    def encode(self, payload: dict[str, Any], *, now: float | None = None) -> str:
        """Serialize, stamp and sign a cursor payload."""
        stamped = {"p": payload, "t": int(now if now is not None else time.time())}
        body = json.dumps(stamped, separators=(",", ":"), sort_keys=True).encode()
        return f"{_b64(body)}.{_b64(self._tag(body))}"

    def decode(self, token: str, *, now: float | None = None) -> dict[str, Any]:
        """Verify and unpack a cursor, or raise `InvalidParameterValue`.

        Every failure — malformed, forged, expired — answers with the same error
        and the same message. Distinguishing them would tell someone probing the
        endpoint which half of their guess was right.
        """
        body_raw, _, tag_raw = token.partition(".")
        if not body_raw or not tag_raw:
            raise InvalidParameterValue("the pagination token is not valid")
        try:
            body = _unb64(body_raw)
            tag = _unb64(tag_raw)
        except ValueError:
            raise InvalidParameterValue("the pagination token is not valid") from None

        # compare_digest, not `==`: a byte-at-a-time comparison leaks how much of
        # a forged tag was right, which is enough to forge the rest one byte at a
        # time given enough attempts.
        if not hmac.compare_digest(tag, self._tag(body)):
            raise InvalidParameterValue("the pagination token is not valid")

        try:
            stamped: Any = json.loads(body)
        except json.JSONDecodeError:
            raise InvalidParameterValue("the pagination token is not valid") from None
        if not isinstance(stamped, dict):
            raise InvalidParameterValue("the pagination token is not valid")

        envelope = cast(dict[str, Any], stamped)
        issued = envelope.get("t")
        payload = envelope.get("p")
        if not isinstance(issued, int) or not isinstance(payload, dict):
            raise InvalidParameterValue("the pagination token is not valid")
        decoded = cast(dict[str, Any], payload)
        if self._ttl is not None:
            age = (now if now is not None else time.time()) - issued
            if age > self._ttl or age < -_CLOCK_SLACK_SECONDS:
                raise InvalidParameterValue("the pagination token is not valid")
        return decoded

    def _tag(self, body: bytes) -> bytes:
        return hmac.new(self._secret, body, sha256).digest()


# A cursor stamped slightly in the future is a clock that drifted, not an attack;
# one stamped an hour ahead is neither, and is refused.
_CLOCK_SLACK_SECONDS = 60


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:  # noqa: BLE001 - binascii raises several types
        raise ValueError("undecodable") from exc
