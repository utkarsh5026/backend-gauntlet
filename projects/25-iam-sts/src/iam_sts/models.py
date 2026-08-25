"""The shared vocabulary: ARNs, principals, requests, decisions.

Plumbing, fully implemented — these are the nouns every vertical passes around,
and re-deriving them per module is how six modules end up with six subtly
different ideas of what "a principal" is.

Two things here are worth reading closely.

`Arn` is a **structured** type, not a string. Every ARN comparison in this
service goes through these six fields, because prefix-matching ARNs is how
`arn:aws:s3:::my-bucket-public` ends up matching a policy written for
`arn:aws:s3:::my-bucket`. Parsing a string into one of these is V2's job, and it
is harder than it looks.

`Decision` has **three** values, not two. "Denied because a statement said so"
and "denied because nothing said anything" are the same outcome and completely
different bugs: the first means someone wrote a `Deny` you did not expect, the
second means the `Allow` you expected is missing or does not match. Collapsing
them into `False` is the single most common way an authorization system becomes
undebuggable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Arn",
    "AuthorizationRequest",
    "AuthorizationResult",
    "ContextValue",
    "Decision",
    "Effect",
    "Identity",
    "PolicyType",
    "Principal",
    "PrincipalType",
    "RequestContext",
]

# A condition key's value. Multi-valued keys are real (`aws:PrincipalTag/team`
# with several tags, `aws:TagKeys`) and are why `ForAllValues:` / `ForAnyValue:`
# exist — a single `str` here would quietly make those operators untestable.
ContextValue = str | list[str]
RequestContext = dict[str, ContextValue]


class Effect(StrEnum):
    """What a statement says. Exactly two, and they are not symmetric."""

    ALLOW = "Allow"
    DENY = "Deny"


class Decision(StrEnum):
    """What the evaluation concluded.

    `IMPLICIT_DENY` is the default answer to every question nobody answered, and
    keeping it distinct from `EXPLICIT_DENY` is a V3 criterion.
    """

    ALLOW = "Allow"
    EXPLICIT_DENY = "ExplicitDeny"
    IMPLICIT_DENY = "ImplicitDeny"

    @property
    def is_allowed(self) -> bool:
        return self is Decision.ALLOW


class PolicyType(StrEnum):
    """The five independent authorities V3 composes.

    Order here is not the evaluation order — deliberately, so that nothing can
    accidentally depend on the enum's declaration order for correctness. V3 owns
    the order, and it belongs in one place.
    """

    IDENTITY = "identity"
    RESOURCE = "resource"
    PERMISSION_BOUNDARY = "permission_boundary"
    SESSION = "session"
    SERVICE_CONTROL = "service_control"
    # The trust policy is a resource policy that happens to live on a role. It is
    # named separately because it is evaluated at AssumeRole time (V4) rather
    # than on the request path.
    TRUST = "trust"


class PrincipalType(StrEnum):
    """What kind of thing is making the request.

    `ASSUMED_ROLE` is a distinct type from `ROLE` on purpose: the role is the
    thing you configure, the assumed role is the thing that shows up in the audit
    log with a session name attached. Confusing the two is why people cannot find
    their own requests in CloudTrail.
    """

    ROOT = "root"
    USER = "user"
    ROLE = "role"
    ASSUMED_ROLE = "assumed_role"
    SERVICE = "service"
    FEDERATED = "federated"


@dataclass(frozen=True, slots=True)
class Arn:
    """A structured ARN: `arn:partition:service:region:account:resource`.

    Frozen and hashable, so it can be part of a decision cache key without being
    re-serialized on every lookup.

    Note `resource` is kept whole rather than split into type and id. The
    separator differs by service — `:` for some, `/` for others, both for a few —
    so splitting it *here* would bake one service's convention into the shared
    vocabulary. Splitting it correctly, per service, is part of V2.
    """

    partition: str
    service: str
    region: str
    account: str
    resource: str

    def __str__(self) -> str:
        return f"arn:{self.partition}:{self.service}:{self.region}:{self.account}:{self.resource}"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making the request, after authentication has succeeded.

    `tags` are here because policy variables (`${aws:PrincipalTag/team}`) and
    attribute-based access control read from them — they are an input to the
    decision, which means they are part of the cache key.
    """

    arn: Arn
    principal_type: PrincipalType
    account_id: str
    # Set only for ASSUMED_ROLE: the name the assumer chose, and what makes one
    # session distinguishable from another in the audit trail.
    session_name: str | None = None
    tags: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def is_root(self) -> bool:
        return self.principal_type is PrincipalType.ROOT


@dataclass(frozen=True, slots=True)
class Identity:
    """The result of authenticating a request (V1, and V4 for session tokens).

    Separate from `Principal` because authentication answers *who* while
    authorization asks *what may they do* — and the extra fields here are
    authentication's, not authorization's. `session_id` in particular exists so
    V6 can revoke a live session without revoking the underlying user.
    """

    principal: Principal
    access_key_id: str
    # Wall-clock expiry for temporary credentials; None for a long-lived key.
    expires_at: float | None = None
    session_id: str | None = None
    # Rides in from the session token (V4) and narrows the chain in V3.
    session_policy: str | None = None
    transitive_tag_keys: tuple[str, ...] = ()

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """One question: may this principal do this thing to that resource?

    Frozen because it is the decision cache's key material (V5). If you find
    yourself wanting to mutate one of these, what you actually want is a second
    request — and the fact that it is a *different* request is exactly the thing
    the cache needs to know.
    """

    principal: Principal
    action: str  # "s3:GetObject" — service prefix and action name
    resource: Arn
    context: RequestContext = field(default_factory=dict[str, ContextValue])

    @property
    def service(self) -> str:
        """The service prefix of the action — `s3` in `s3:GetObject`."""
        return self.action.split(":", 1)[0]


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    """The answer, and — just as importantly — why.

    "Why" is not a nicety. It is what V6's audit trail records, what the SPEC's
    observability section aggregates into deny-reason graphs, and what V6's
    simulator has to reproduce exactly. A result carrying only a boolean makes
    all three of those impossible.
    """

    decision: Decision
    # Which authority ended the evaluation, and which statement inside it.
    deciding_policy_type: PolicyType | None = None
    deciding_policy_id: str | None = None
    deciding_statement_id: str | None = None
    # Human-readable, safe to log, safe to aggregate as a metric label — so keep
    # it a bounded set of phrases rather than an interpolated string.
    reason: str = ""
    # Which condition keys were actually consulted. V5 requires the cache key to
    # cover every input the decision depended on; this is that list.
    consulted_context_keys: tuple[str, ...] = ()
    cached: bool = False
    evaluation_micros: float = 0.0

    @property
    def is_allowed(self) -> bool:
        return self.decision.is_allowed
