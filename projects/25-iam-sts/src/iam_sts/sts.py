"""V4 — STS: credentials that expire, for a principal that does not exist.

`AssumeRole` is the primitive that makes the rest survivable. Instead of handing
someone a long-lived key, you publish a **role** — a set of permissions with a
**trust policy** saying who may briefly *become* it. What comes back is a triple:

    AccessKeyId      ASIA...           (note ASIA, not AKIA — temporary)
    SecretAccessKey  <ephemeral>
    SessionToken     <the interesting part>

The session token is where the design lives. It is not a database handle. It is a
**self-describing, integrity-protected bundle** carrying the assumed identity,
the session policy, the session tags and the expiry — so the thing verifying it
needs no lookup at all. That is precisely how authorization gets fast: V5's hot
path can know who you are from the bytes you sent, with zero round trips.

And it is precisely why V6 is hard. A credential that requires no lookup to
*accept* requires an explicit mechanism to *reject* — you removed the very step
where a revocation check would naturally have lived. That trade is the concept:
you bought latency with statelessness and paid for it in revocation, and it is
worth being able to say out loud that you made that trade deliberately.

Then the sharp edges, each of which is a criterion:

  * **Role chaining** truncates you to one hour, no matter what you asked for.
    People discover this at hour two of a long job.
  * **`ExternalId`** exists for exactly one reason: the confused deputy. Your
    monitoring vendor holds a role in a thousand customer accounts; without a
    secret only *you* and *they* know, customer B can ask the vendor to use its
    access to customer A. The `ExternalId` is what makes the vendor's role
    unusable on anyone else's behalf.
  * A role trusting `{"AWS": "*"}` is a public front door, and it is one
    copy-pasted example away at all times.

Scaffold state: the session model and the codec's shape are here; minting,
resolving and the trust check raise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

from .config import Settings
from .models import Identity, Principal
from .policy import PolicyDocument

__all__ = [
    "AssumedRoleCredentials",
    "STS_TEMPORARY_KEY_PREFIX",
    "SecurityTokenService",
    "Session",
    "SessionTokenCodec",
]

log = structlog.get_logger(__name__)

# Real temporary access keys start ASIA; long-lived ones start AKIA. The prefix
# is not decorative — it is how a human reading a log, and a scanner reading a
# repository, can tell at a glance whether a leaked credential expires on its own.
STS_TEMPORARY_KEY_PREFIX = "ASIA"


@dataclass(frozen=True, slots=True)
class Session:
    """Everything an assumed-role session is, and everything the token carries.

    Frozen, because the token is a snapshot: once minted, nothing here can change
    for the life of the credential. If you find yourself wanting to mutate a
    session, what you actually want is either a new `AssumeRole` or — for the
    "make it stop" case — V6's revocation registry.
    """

    session_id: str
    role_arn: str
    session_name: str
    # The identity that performed the AssumeRole. Kept so the audit trail can
    # answer "who is actually behind this session", which is the question every
    # incident eventually asks.
    assumed_by_arn: str
    issued_at: float
    expires_at: float
    # Narrows the role's permissions for this session only. Rides in the token.
    session_policy: PolicyDocument | None = None
    tags: dict[str, str] = field(default_factory=dict[str, str])
    # Tags that survive a *chained* assume. Non-transitive tags are dropped at
    # the next hop, which is what stops a tag from silently propagating through
    # a chain nobody audited.
    transitive_tag_keys: tuple[str, ...] = ()
    # 0 for a direct assume from a user; incremented per hop.
    chain_depth: int = 0
    source_identity: str | None = None

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at


@dataclass(frozen=True, slots=True)
class AssumedRoleCredentials:
    """What `AssumeRole` hands back.

    `secret_access_key` is a plain `str` here rather than a `SecretStr` because
    it is about to be serialized into a response body on purpose — this is the
    one place in the service where a secret legitimately crosses the wire, and
    wrapping it would only add a `.get_secret_value()` that reads as approval.
    Everywhere else, wrap it.
    """

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: float
    assumed_role_arn: str


class SessionTokenCodec:
    """Encodes a `Session` into an opaque, tamper-evident token — and back.

    Two properties, and they are different:

    * **Integrity** is required. A holder must not be able to change their role,
      their expiry, or their session policy. This is a MAC over the payload.
    * **Confidentiality** is a decision. Real STS tokens are opaque and appear to
      be encrypted; you could equally ship a signed-but-readable payload. Signed
      and readable is easier to debug and leaks the session policy to whoever
      holds the token — which is usually the principal it describes, so the leak
      may be nothing at all. Decide, and record it in `docs/25-design.md`.

    What is *not* a decision: rolling your own construction. Use `hmac` with
    `compare_digest`, or an AEAD from `cryptography` — the difference between a
    MAC and "hash the payload with the key prepended" is a length-extension
    attack, and it has been the answer to this exact question since 2009.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def encode(self, session: Session) -> str:
        """Serialize and integrity-protect a session."""
        # TODO(V4): serialize the session, MAC it, and encode to something
        # URL-safe (it travels in a header and, for presigned URLs, in a query
        # string).
        #
        # Include a **key id** in the token. The rotation criterion in the
        # horizontal checklist needs to accept two keys during an overlap window,
        # and it cannot do that if the token does not say which one signed it.
        # Adding the field later means invalidating every live session.
        #
        # Include the **issue time** as well as the expiry: V6 revokes "all
        # sessions for this role issued before T", which is the mechanism the
        # real service exposes as "Revoke sessions", and it needs the issue time
        # to be inside the token.
        raise NotImplementedError("V4: encode and integrity-protect a session token")

    def decode(self, token: str) -> Session:
        """Verify and deserialize a session token, or raise."""
        # TODO(V4): verify **before** parsing. It is tempting to deserialize
        # first — you need the key id to pick the key — but everything past the
        # key id must be treated as hostile until the MAC checks out. Parse only
        # the framing, verify, then parse the payload.
        #
        # Compare with `hmac.compare_digest`, and raise the *same* error for a
        # bad MAC and a malformed token: distinguishing them tells an attacker
        # when they have the framing right.
        raise NotImplementedError("V4: verify and decode a session token")


class SecurityTokenService:
    """Mints and resolves temporary credentials.

    `resolve` is the function V1 calls to turn an `ASIA...` key id into a secret,
    which is what makes the two verticals fit together: for a long-lived key that
    lookup hits the identity store, and for a temporary one it is a *decode of
    the token the client already sent* — no store, no round trip.
    """

    def __init__(self, settings: Settings, codec: SessionTokenCodec) -> None:
        self._settings = settings
        self._codec = codec
        # Live sessions, for observability and for V6's revocation. Note that
        # authentication does **not** consult this — the token verifies on its
        # own, which is the entire point of V4 and the entire problem of V6.
        self._sessions: dict[str, Session] = {}

    async def assume_role(
        self,
        *,
        caller: Identity,
        role_arn: str,
        session_name: str,
        trust_policy: PolicyDocument,
        duration_seconds: float | None = None,
        external_id: str | None = None,
        session_policy: PolicyDocument | None = None,
        tags: dict[str, str] | None = None,
        transitive_tag_keys: tuple[str, ...] = (),
    ) -> AssumedRoleCredentials:
        """Check the trust policy and mint credentials for the role."""
        # TODO(V4): the order, and why each step is where it is:
        #
        #   1. **Trust policy first.** It is a resource policy that lives on the
        #      role, and it decides *who may assume*. Evaluate the caller against
        #      it with V3's machinery — do not write a second, simpler matcher
        #      here, because a trust policy with a condition block is exactly
        #      where `ExternalId` is enforced.
        #   2. **ExternalId** comes out of that evaluation, as a condition on
        #      `sts:ExternalId`. It is not a separate `if`: making it a condition
        #      is what lets a customer write the trust policy that protects them.
        #   3. **Duration:** requested, capped by the role's maximum, capped again
        #      by `chained_session_max_duration_seconds` when
        #      `caller.session_id is not None` — that is the chaining truncation.
        #   4. **Chain depth:** refuse past `max_role_chain_depth`. Without a
        #      bound, role chaining is an unbounded credential-laundering path
        #      through however many accounts trust each other transitively.
        #   5. **Tags:** merge, then drop the caller's non-transitive tags. Only
        #      transitive ones survive the hop.
        #   6. Mint, encode, record.
        #
        # Two failures must stay distinguishable, because their fixes are
        # different: "the trust policy does not name you" (fix the role) versus
        # "you lack sts:AssumeRole" (fix the caller's identity policy).
        raise NotImplementedError("V4: check the trust policy and mint credentials")

    async def resolve(self, access_key_id: str, session_token: str | None) -> Identity:
        """Turn a temporary access key id + token into the identity it represents."""
        # TODO(V4): decode the token, check expiry against the clock, confirm the
        # token's own access key id matches the one presented — otherwise a token
        # from one session can be paired with a key id from another.
        #
        # Return the derived secret for V1 to verify the signature with. Where
        # that secret comes from is the design question: deriving it from the
        # session (so it need not be stored) keeps the whole path stateless and
        # is why real STS can verify a temporary credential in any region within
        # seconds of minting it.
        raise NotImplementedError("V4: resolve a temporary credential to an identity")

    def caller_identity(self, identity: Identity) -> Principal:
        """What `GetCallerIdentity` reports.

        The call that ends most IAM debugging sessions, because the answer is
        frequently not the one the caller expected.
        """
        # TODO(V4): for an assumed role this must report the **assumed-role ARN**
        # (`arn:aws:sts::<account>:assumed-role/<role>/<session-name>`), not the
        # role ARN and definitely not the underlying user's. Those are three
        # different strings that all feel like "who I am", and only one of them
        # is what appears in the audit trail — which is exactly why people cannot
        # find their own requests in it.
        raise NotImplementedError("V4: report the caller identity")

    async def reap_expired(self) -> int:
        """Drop expired sessions from the table. Returns how many.

        Housekeeping only. Expiry is *enforced* by the token's own claim, not by
        this — a session missing from the table is not thereby revoked, and a
        session present in it is not thereby valid. Confusing bookkeeping with
        enforcement is how a reaper becomes a security control by accident.
        """
        # TODO(V4): sweep. Bounded work per sweep, so a large table does not turn
        # this into a periodic stall on the same event loop serving decisions.
        raise NotImplementedError("V4: reap expired sessions")

    def live_session_count(self) -> int:
        """For `/metrics`. Plumbing."""
        return len(self._sessions)
