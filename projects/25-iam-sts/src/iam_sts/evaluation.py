"""V3 — The evaluation chain: five authorities, any one of which can say no.

"Does the identity policy allow it?" is not the question. The real one runs a
gauntlet, and the layers were written by different people with different
authority over you:

    SCP                  the organization's ceiling — you cannot exceed it
    Resource policy      the owner of the thing being touched
    Permission boundary  the maximum your admin will let you be granted
    Session policy       the narrowing you accepted when you assumed the role
    Identity policy      what you were actually given

An explicit `Deny` anywhere ends the evaluation immediately. An `Allow` is only an
allow when every layer that *must* allow does — and **which layers must allow
changes depending on whether the call is same-account or cross-account.**

That asymmetry is the most misunderstood rule in AWS and it is worth stating
plainly, because it is the thing this vertical exists to teach:

    same-account:   identity policy OR resource policy grants it
    cross-account:  identity policy AND resource policy — both, independently

The reason is ownership. Within one account there is a single administrative
authority, so either of its two voices may speak for it. Across accounts there
are two authorities and neither may grant on the other's behalf — your admin
cannot give you access to my bucket, and I cannot give your credentials
permissions your admin withheld. Both must agree, separately. That is the whole
security model of cross-account access, in one conjunction.

Build the composition here. The matcher it calls is V2's; keeping "does this
statement apply" apart from "what do all these policies conclude" is what makes
either one testable.

Scaffold state: the shapes and the vocabulary are here; the evaluation raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from .models import AuthorizationRequest, AuthorizationResult, PolicyType
from .policy import ConditionEvaluator, PolicyDocument

__all__ = ["PolicyEvaluator", "PolicySet"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class PolicySet:
    """Everything that gets a vote on one request.

    Assembled by the authorizer (V5) from the store, then handed here. Note this
    is deliberately a *value*: the evaluator does no lookups of its own, which is
    what makes it a pure function — and a pure function is what V6's simulator
    needs in order to give the same answer as the live path without performing
    the action.
    """

    # Attached to the principal. Several, because a user can have many.
    identity: tuple[PolicyDocument, ...] = ()
    # On the resource being touched. At most one, and often absent — most
    # resources have no resource policy at all, which is not the same as having
    # one that denies.
    resource: PolicyDocument | None = None
    # The account that owns the resource. Decides same- vs cross-account, so it
    # is required rather than inferred: inferring it from the resource ARN is
    # right until it is a service that puts something else in that field.
    resource_owner_account: str = ""
    permission_boundary: PolicyDocument | None = None
    # Rides in on the session token (V4). Can only narrow.
    session: PolicyDocument | None = None
    scps: tuple[PolicyDocument, ...] = ()


@dataclass(slots=True)
class LayerVerdict:
    """What one layer concluded, kept separate so the composition is readable.

    Three-valued on purpose — `allowed`, `denied`, and *silent* — because a layer
    that says nothing composes very differently from one that says no. An SCP
    that is silent blocks; a resource policy that is silent does not, in the
    same-account case. Collapsing silence into a boolean is how the two get
    confused.
    """

    explicit_deny: bool = False
    explicit_allow: bool = False
    policy_id: str | None = None
    statement_id: str | None = None
    consulted_keys: tuple[str, ...] = field(default_factory=tuple[str, ...])

    @property
    def is_silent(self) -> bool:
        return not self.explicit_deny and not self.explicit_allow


class PolicyEvaluator:
    """Composes the five authorities into one decision, with its reason.

    Pure: no I/O, no clock, no store. Everything it needs arrives in the
    `PolicySet`. That constraint is doing real work — it is what lets V5 cache
    the result safely (the inputs are all visible), and what lets V6's simulator
    reuse this exact code rather than growing a second implementation that drifts.
    """

    def __init__(self, conditions: ConditionEvaluator) -> None:
        self._conditions = conditions

    def evaluate(self, request: AuthorizationRequest, policies: PolicySet) -> AuthorizationResult:
        """The whole decision. Deny by default, and say why."""
        # TODO(V3): compose the layers. A workable order, and the reasoning for it:
        #
        #   1. **Any explicit Deny, anywhere, at any layer → done.** Check this
        #      first and check it across every layer, because a deny that only
        #      wins within its own layer is not a deny that wins.
        #   2. **SCP:** must explicitly allow, or the answer is deny — for every
        #      principal in the account including root. An SCP is a ceiling, not
        #      a grant: it never gives permission, it only fails to take it away.
        #   3. **Resource policy vs identity policy:** the OR/AND asymmetry in
        #      the module docstring. `policies.resource_owner_account` versus
        #      `request.principal.account_id` is what selects which rule applies.
        #   4. **Permission boundary:** if one exists it must also allow. Like an
        #      SCP it never grants — a principal with only a boundary and no
        #      identity policy can do nothing at all.
        #   5. **Session policy:** same shape as the boundary. It can only narrow
        #      what the assumed role already had.
        #   6. Nothing matched → `IMPLICIT_DENY`, and say so distinctly.
        #
        # Populate the deciding statement on **every** path, allows included. A
        # result that says "allowed" without naming which of forty statements
        # allowed it is a result nobody can audit and nobody can debug, and both
        # V6's audit trail and the SPEC's observability section require it.
        #
        # Wrap the body so that any exception becomes a deny with a reason. That
        # is the fail-closed criterion, and it is a criterion because the natural
        # shape of this function — a chain of early returns — has an equally
        # natural bug where an exception in layer three skips layers four and
        # five and lands somewhere optimistic.
        raise NotImplementedError("V3: compose the five layers into one decision")

    def _evaluate_layer(
        self,
        request: AuthorizationRequest,
        documents: tuple[PolicyDocument, ...],
        policy_type: PolicyType,
    ) -> LayerVerdict:
        """Fold one layer's documents into a single verdict."""
        # TODO(V3): walk every statement of every document in this layer.
        #
        # Do not short-circuit on the first `Allow`: a later statement in the
        # same layer may `Deny`, and deny wins. You *may* short-circuit on the
        # first `Deny` — nothing after it can change the answer. That asymmetry
        # is worth writing down, because it is the shape of the whole model.
        #
        # Accumulate `consulted_keys` across statements. V5 needs the union, and
        # a key consulted by a statement that did not match still influenced the
        # decision — it is why that statement did not match.
        raise NotImplementedError("V3: fold one layer's statements into a verdict")

    def _is_cross_account(self, request: AuthorizationRequest, policies: PolicySet) -> bool:
        """Is the principal in a different account from the resource's owner?"""
        # TODO(V3): compare the principal's account with the resource owner's.
        #
        # Trickier than `!=` suggests. A service principal (`lambda.amazonaws.com`)
        # has no account. An assumed role's account is the role's, not the
        # assumer's. And the resource's owning account is not always the account
        # field of its ARN — some services leave it empty. Decide what each of
        # those means here, and record it, because every one of them decides
        # whether the rule above is OR or AND.
        raise NotImplementedError("V3: decide same-account versus cross-account")
