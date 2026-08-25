"""V6 — Revocation & the audit trail: the two questions a postmortem asks.

Nobody asks whether IAM was fast. They ask two things.

**Make it stop.** A leaked key, a live session, a policy someone should never
have had — dead, now. This is genuinely hard here, and the reason is V4: the
whole point of the self-describing session token was that verifying it needs no
lookup. You deliberately removed the step where a revocation check would
naturally have lived, so you have to put one back — and every design that puts it
back costs some of the latency you bought. That trade is the vertical.

**Who did what.** Every decision, its inputs, and the statement that decided it,
in a trail an auditor can replay months later. The hard part is not writing lines
to a file; it is writing them without putting the audit path on the hot path,
without ever emitting a secret, and in a form where a consumer can *prove* it
saw everything.

Building this last is the right order. It is the vertical that makes the other
five accountable: V3's deciding statement is only useful if something records it,
and V5's cache TTL is only a promise if something measures when a revoke takes
effect.

Scaffold state: the record shape and the queue's bounds are here; recording,
flushing, revoking and simulating raise.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .config import Settings
from .evaluation import PolicyEvaluator, PolicySet
from .models import AuthorizationRequest, AuthorizationResult, Decision, Identity

__all__ = ["AuditLog", "AuditRecord", "PolicySimulator", "RevocationRegistry"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One decision, as an auditor will read it months from now.

    Every field is here because someone asks for it during an incident:

    * `sequence` — monotonic per writer, so a consumer can detect a **gap**. A
      log you cannot prove is complete is a log that cannot exonerate anyone,
      which is half of what an audit trail is for.
    * `deciding_*` — *why*, not just *what*. "Denied" sends someone hunting
      through forty statements; "denied by statement `DenyUnlessMFA` in the
      boundary" ends the conversation.
    * `consulted_context_keys` — which inputs mattered. This is what makes a
      decision reproducible later, when the source IP and the time of day that
      produced it are long gone.
    * `cached` — whether this decision was *made* now or merely *served* now. An
      auditor reconstructing what the policies said at 04:12 needs to know that a
      decision at 04:12 may have been computed at 04:11.

    Frozen and flat on purpose: a record with a mutable field is a record that
    can be edited between the decision and the write.
    """

    sequence: int
    timestamp: float
    request_id: str
    principal_arn: str
    action: str
    resource: str
    decision: Decision
    deciding_policy_type: str | None = None
    deciding_policy_id: str | None = None
    deciding_statement_id: str | None = None
    reason: str = ""
    consulted_context_keys: tuple[str, ...] = ()
    cached: bool = False


class AuditLog:
    """Bounded, asynchronous audit writer.

    Bounded is the interesting word. An audit buffer that grows without limit
    takes down the authorizer it was auditing, which converts a logging problem
    into an outage. But silently dropping records converts it into a *compliance*
    problem, and one that nobody notices. So the SPEC asks for the third option:
    **shed explicitly** — refuse, count the refusal, and make the gap visible in
    the stream so a consumer can see exactly what it did not get.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._max_queue = settings.audit_queue_size
        self._sequence = 0
        self.shed_total = 0

    async def record(self, record: AuditRecord) -> None:
        """Enqueue one decision. Must not block the caller."""
        # TODO(V6): enqueue without awaiting the write.
        #
        # "Off the hot path" is a measured criterion (< 5% of p99), so the
        # enqueue has to be genuinely cheap: no formatting, no serialization, no
        # `await` on anything that can block. Format at flush time, in the
        # background task, where the cost is amortized over a batch.
        #
        # On a full queue, shed and count — never `await queue.put()`, which
        # applies backpressure from the *audit* system to the *authorization*
        # system and makes a slow disk into an authorization outage.
        raise NotImplementedError("V6: enqueue an audit record without blocking")

    async def flush(self) -> int:
        """Write queued records out. Returns how many. Called by the flush loop."""
        # TODO(V6): drain a batch and write it.
        #
        # File I/O is blocking, and this runs on the event loop that is also
        # serving 20k decisions/sec — so the write itself belongs in a thread (or
        # a separate process). Getting this wrong is the classic version of the
        # SPEC's "no blocking call on the event loop" criterion: it will not show
        # up in a test and it will show up as latency spikes under load.
        #
        # The tamper-evidence criterion is decided here too. A hash chain — each
        # record covering the previous record's hash — makes any alteration or
        # removal detectable by re-walking the file, and costs one hash per
        # record. Whatever you choose, `docs/25-design.md` records what it does
        # and does not protect against (notably: it detects tampering, it does
        # not prevent it, and it is worthless if the attacker can rewrite the
        # whole file and recompute the chain).
        raise NotImplementedError("V6: flush queued audit records off the loop")

    def depth(self) -> int:
        """For `/metrics`. Plumbing."""
        return 0


class RevocationRegistry:
    """Makes a credential stop working before it would have expired.

    This is where V4's bill comes due. A session token verifies on its own with
    no lookup, so the only way to reject one early is to consult *something* —
    and whatever that something is, it now sits on the authentication path you
    worked to keep stateless.

    The design space, roughly in order of cost:

    * **A revoked-session set.** Precise, and a lookup per request.
    * **A per-role "sessions issued before T are invalid" watermark.** This is
      what the real service's "Revoke sessions" does, and it is why the session
      token has to carry its issue time. One comparison per request against a
      small map, and it revokes everything for a role at once — you cannot
      revoke a single session with it.
    * **Ride the decision cache TTL.** Free, and bounded by
      `decision_cache_ttl_seconds` — which is exactly the sentence that makes
      that TTL a security parameter.

    Most real systems use more than one. Pick, measure, and write down the window
    each choice actually delivers.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def revoke_access_key(self, access_key_id: str) -> None:
        """Deactivate a long-lived key immediately."""
        # TODO(V6): record it, and make the *authentication* path consult this.
        #
        # Note the criterion says "the next request using it fails within the
        # documented window **at full load**" — under load there are in-flight
        # requests that already authenticated, and cached decisions made under
        # the old credential. Both are part of the window, and both are easy to
        # forget when the idle-path test passes instantly.
        raise NotImplementedError("V6: revoke a long-lived access key")

    def revoke_sessions_for_role(self, role_arn: str, issued_before: float) -> None:
        """Invalidate every session for a role issued before an instant."""
        # TODO(V6): store the watermark; `is_revoked` compares against it.
        #
        # `issued_before` rather than "all sessions" on purpose: an operator
        # revoking at 14:03 wants the sessions that exist *now* dead, and does
        # not want to break the session the incident responder creates at 14:04.
        raise NotImplementedError("V6: set a per-role revocation watermark")

    def is_revoked(self, identity: Identity) -> bool:
        """Consulted on the authentication path. Must be very cheap."""
        # TODO(V6): key check, then session check, then the role watermark.
        #
        # This runs on every authenticated request, so its cost is added to every
        # request in the company. A dict lookup or two is fine; anything that
        # touches the network here has just put a round trip in front of every
        # API call in the account.
        raise NotImplementedError("V6: decide whether this identity has been revoked")


class PolicySimulator:
    """`SimulatePrincipalPolicy` — the answer without the action.

    The criterion that matters is **parity**: the same decision *and the same
    deciding statement* as the live path. That is why this holds a
    `PolicyEvaluator` rather than reimplementing evaluation — a simulator that is
    a second implementation is a simulator that will drift, and it will drift
    silently, and the day it matters is the day someone trusts it about
    production.

    Which is also why the simulator deliberately does **not** use V5's cache: it
    must answer what the policies say *now*, not what they said within the TTL.
    Same evaluation, different freshness requirement.
    """

    def __init__(self, evaluator: PolicyEvaluator) -> None:
        self._evaluator = evaluator

    async def simulate(
        self, request: AuthorizationRequest, policies: PolicySet
    ) -> AuthorizationResult:
        """Evaluate without performing the action or touching the cache."""
        # TODO(V6): call the evaluator directly, uncached.
        #
        # Worth adding beyond the bare answer, because it is what makes a
        # simulator useful rather than merely correct: report every statement
        # that was *considered*, not only the one that decided. "Why did nothing
        # match?" is the actual question people bring to a simulator, and
        # `ImplicitDeny` alone does not answer it.
        raise NotImplementedError("V6: simulate a decision with the live evaluator")
