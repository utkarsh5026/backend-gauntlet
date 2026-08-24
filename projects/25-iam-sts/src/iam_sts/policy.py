"""V2 — The policy language: a tiny grammar with an enormous blast radius.

A policy is JSON, which is why everyone treats it as configuration. It is a
language, and it has all the hazards of one:

    {"Effect": "Allow", "Action": "s3:Get*", "Resource": "arn:aws:s3:::bucket/*",
     "Condition": {"StringEquals": {"aws:PrincipalTag/team": "${aws:username}"}}}

Four attacker-relevant decisions in one statement. `s3:Get*` is one character
from `s3:*`. The resource wildcard is a claim about every object that will ever
exist in that bucket. The condition reads a key that the request may simply not
contain — and whether an absent key passes or fails depends on the operator, in a
way nobody guesses correctly the first time. And the policy variable interpolates
attacker-influenced text into the middle of a pattern.

This module owns **one statement against one request**. Composing many policies
from several authorities is V3's problem; keeping the two apart is what makes
either testable.

The single highest-value thing to build here is a table-driven test. The matcher
is a pure function of (statement, request) → match, which means the entire
vertical can be pinned by a list of tuples — and that list is where you encode
every trap in this docstring before you have any code that could pass it.

Scaffold state: the document model is defined; parsing, matching and condition
evaluation raise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from .config import Settings
from .models import Arn, AuthorizationRequest, Effect, RequestContext

__all__ = [
    "ConditionEvaluator",
    "PolicyDocument",
    "Statement",
    "StatementMatch",
    "interpolate_variables",
    "match_statement",
    "matches_action",
    "matches_arn",
    "parse_arn",
    "parse_policy",
]

log = structlog.get_logger(__name__)

# The only policy language version that supports policy variables. `2008-10-17`
# exists, is still accepted by the real service, and silently does *not*
# interpolate `${...}` — it treats it as a literal. That is a genuinely nasty
# trap: an old version string turns a variable into a constant, and a policy that
# looks scoped to one user matches nobody, or everybody, depending on where the
# variable sat.
POLICY_VERSION = "2012-10-17"


@dataclass(frozen=True, slots=True)
class Statement:
    """One statement. Note that every list field has a `Not` twin.

    `Not*` is not sugar. `NotAction: ["iam:*"]` means *every action in AWS except
    IAM's* — including all the services that did not exist when it was written.
    That is the trap the SPEC asks for a dedicated test about: the author reads it
    as "deny IAM", the evaluator reads it as "grant the universe minus IAM", and
    those differ by every service AWS ships next year.

    Both a field and its `Not` twin being set is invalid, not merely unusual, and
    `parse_policy` should refuse it rather than pick one.
    """

    effect: Effect
    sid: str | None = None
    actions: tuple[str, ...] = ()
    not_actions: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    not_resources: tuple[str, ...] = ()
    # Only meaningful in a resource or trust policy — an identity policy with a
    # Principal is malformed, because the identity it is attached to *is* the
    # principal.
    principals: tuple[str, ...] = ()
    not_principals: tuple[str, ...] = ()
    # {"StringEquals": {"aws:username": ["alice"]}} — operator, key, values.
    conditions: dict[str, dict[str, tuple[str, ...]]] = field(
        default_factory=dict[str, dict[str, tuple[str, ...]]]
    )


@dataclass(frozen=True, slots=True)
class PolicyDocument:
    """A parsed, validated policy.

    Frozen because V5 caches its *compiled* form, and a mutable document behind a
    compiled cache is a cache that silently serves a policy nobody can see any
    more. Editing a policy produces a new document and an invalidation.
    """

    version: str
    statements: tuple[Statement, ...]
    # Set by the store, so a decision can name which policy decided it.
    policy_id: str = ""


@dataclass(frozen=True, slots=True)
class StatementMatch:
    """Did this statement apply, and what did it say?

    `consulted_keys` exists for V5: the decision cache key must include every
    input the decision depended on, and the condition keys a statement actually
    read *are* those inputs. A cache key built from principal/action/resource
    alone is wrong the moment a policy conditions on source IP.
    """

    matched: bool
    effect: Effect | None = None
    sid: str | None = None
    consulted_keys: tuple[str, ...] = ()


def parse_arn(value: str) -> Arn:
    """Parse `arn:partition:service:region:account:resource` into its fields.

    Six colon-separated parts, and the sixth may itself contain colons.
    """
    # TODO(V2): split on `:` with a bounded maxsplit so the resource keeps its own
    # colons, then validate every field.
    #
    # The reason this is a vertical criterion and not a one-liner: this function
    # is the boundary between "a string an attacker sent" and "a structured thing
    # the matcher trusts". Get it wrong and a crafted resource name escapes into
    # the account or service field, and a policy scoped to one account matches
    # another. Reject empty partition or service, reject an account that is not
    # digits or empty (some services legitimately have no account), and never
    # accept a fragment with fewer than six parts by padding it.
    raise NotImplementedError("V2: parse an ARN structurally")


def matches_action(pattern: str, action: str) -> bool:
    """Does an `Action` pattern match a concrete action like `s3:GetObject`?"""
    # TODO(V2): glob semantics — `*` matches any run, `?` matches one character.
    #
    # The detail the SPEC calls out: **case sensitivity is not uniform**. Action
    # matching in IAM is case-insensitive, resource matching generally is not,
    # and treating them the same is a real vulnerability in one direction and a
    # baffling non-match in the other. Decide, test, and record which is which.
    #
    # Do not reach for `re` with an interpolated pattern. A user-supplied policy
    # string compiled into a regex is a ReDoS with extra steps, and the SPEC has
    # a bounded-cost criterion aimed squarely at this. `fnmatch` has its own
    # surprises (`[seq]` character classes you did not intend to support); the
    # honest option is a small explicit matcher you can reason about.
    raise NotImplementedError("V2: glob-match an action pattern")


def matches_arn(pattern: str, target: Arn) -> bool:
    """Does a `Resource`/`Principal` ARN pattern match a concrete ARN?"""
    # TODO(V2): match **per segment**, not as one flat string.
    #
    # This is the criterion about ARN confusables. Flattening both sides to a
    # string and globbing gives you `arn:aws:s3:::my-bucket*` matching
    # `arn:aws:s3:::my-bucket-public-oops` — which is correct! — but it *also*
    # gives you patterns whose `*` slides across a `:` boundary and matches a
    # different account entirely. Segment-wise matching makes that impossible by
    # construction; string matching makes it a test you have to remember to write.
    raise NotImplementedError("V2: match an ARN pattern segment-wise")


def interpolate_variables(template: str, context: RequestContext) -> str:
    """Replace `${aws:username}` / `${aws:PrincipalTag/team}` from the context."""
    # TODO(V2): interpolate, and **fail closed** on an unresolvable variable.
    #
    # Failing closed is the whole criterion. If `${aws:PrincipalTag/team}` is
    # missing and you substitute an empty string, a resource pattern of
    # `.../home/${aws:PrincipalTag/team}/*` becomes `.../home//*` — which may
    # well match everything. Raising, or returning a pattern that provably
    # matches nothing, are both defensible; substituting nothing is not.
    #
    # Also handle the escapes: `${*}`, `${?}` and `${$}` exist precisely so a
    # policy can contain a literal `*` that is not a wildcard.
    raise NotImplementedError("V2: interpolate policy variables, failing closed")


class ConditionEvaluator:
    """Evaluates a statement's `Condition` block against the request context.

    The structure is three levels deep and each level composes differently:

        Condition -> operator -> key -> [values]
                     AND         AND    OR

    Every operator must pass; within an operator every key must pass; within a
    key any one value passing is enough. Getting one of those three backwards
    produces a policy that is *mostly* right, which is the worst kind.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def evaluate(
        self, conditions: dict[str, dict[str, tuple[str, ...]]], context: RequestContext
    ) -> tuple[bool, tuple[str, ...]]:
        """Return (all conditions satisfied, the context keys consulted)."""
        # TODO(V2): dispatch on the operator, and get the three tricky families
        # right rather than the twenty easy ones:
        #
        #  * **Absent keys.** `StringEquals` on a key not in the request fails.
        #    `StringNotEquals` on that same absent key *passes* — vacuously, and
        #    that is how an absent-key condition accidentally grants access. The
        #    `...IfExists` suffix makes the pass explicit and intentional.
        #  * **Set operators.** `ForAllValues:` and `ForAnyValue:` change what a
        #    multi-valued key means. `ForAllValues` over an *empty* key is true —
        #    another vacuous pass worth a test with a rude name.
        #  * **Types.** `StringEquals` on a numeric key, or a date operator on
        #    something unparseable, must be a **deny**, never a crash and never a
        #    coerced comparison that happens to succeed.
        #
        # Return the consulted keys as well as the verdict: V5's cache key needs
        # them, and reconstructing them later means reimplementing this dispatch.
        raise NotImplementedError("V2: evaluate the condition block")


def parse_policy(raw: str | dict[str, Any], settings: Settings) -> PolicyDocument:
    """Parse and validate a policy document — at **write** time.

    Raises `MalformedPolicyDocument` / `LimitExceeded` rather than storing
    anything questionable.
    """
    # TODO(V2): validate before storing. Everything on this list is a criterion:
    #
    #   * size against `max_policy_bytes`, statements against
    #     `max_statements_per_policy`, condition keys against
    #     `max_condition_keys_per_statement`;
    #   * `Version` is a known one — and note the 2008 version does not
    #     interpolate variables, so accepting it silently changes what a policy
    #     means;
    #   * `Effect` is exactly `Allow` or `Deny`, case-sensitive;
    #   * a field and its `Not` twin are never both present;
    #   * `Action`/`Resource` accept a bare string *or* a list (the real format
    #     allows both, and normalizing here means the matcher only sees one);
    #   * nesting depth is bounded — `json.loads` on deeply nested input is a
    #     stack overflow waiting to be someone's denial of service.
    #
    # The reason this belongs at write time rather than evaluation time: a policy
    # that only fails when evaluated fails on the hot path, where the only safe
    # answer is to deny — and a whole account failing closed at once looks exactly
    # like an outage, at the least convenient moment.
    raise NotImplementedError("V2: parse and validate a policy document")


def match_statement(
    statement: Statement,
    request: AuthorizationRequest,
    evaluator: ConditionEvaluator,
) -> StatementMatch:
    """Does this statement apply to this request?

    Applies means all four of: the action matches, the resource matches, the
    principal matches (where relevant), and every condition holds.
    """
    # TODO(V2): all four must hold, and the `Not` variants invert each test
    # *before* the conjunction, not after.
    #
    # Return `consulted_keys` from the condition evaluation even when the
    # statement does not match — V5 caches denials too, and a cached denial needs
    # the same key completeness as a cached allow, or the first policy change
    # that flips it will not invalidate it.
    raise NotImplementedError("V2: match one statement against one request")
