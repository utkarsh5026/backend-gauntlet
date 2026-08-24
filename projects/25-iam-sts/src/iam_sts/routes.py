"""The two surfaces: the AWS-shaped API, and the authorization endpoint.

**`public_router` (:9025)** is the Query protocol — one endpoint, and the verb
arrives as an `Action` parameter rather than in the path. That looks strange
until you notice it is 2006's answer to "how do you version an API that will
outlive several generations of HTTP fashion", and it is still running.

Every request here is authenticated **before** it is dispatched. Not after, not
alongside: an unauthenticated caller must learn nothing at all, including whether
an action or an entity exists. That ordering is the reason the scaffold's smoke
tests cannot reach the dispatcher yet — V1 is the front door, and there is
deliberately no way around it.

**`authz_router` (:9026)** is what projects 23, 24 and 06 call. It is a separate
listener for the same reason real IAM separates its planes: the hot path must not
share a bottleneck with people editing policies, and you want to be able to
benchmark it on its own. Right now it is unauthenticated and loopback-bound —
authenticating it is a horizontal checklist item, and until it is done, anything
that can reach that port can ask for any decision.

The routing, the parameter parsing and the sequencing are wired. Every step that
decides something calls into a vertical, which raises until you build it.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .errors import InvalidAction, MissingAction, MissingAuthenticationToken
from .models import Arn, AuthorizationRequest, ContextValue, Identity
from .policy import parse_arn
from .sigv4 import SignedRequest
from .state import AppState

__all__ = ["authorization_request", "authz_router", "public_router", "result_to_body"]

log = structlog.get_logger(__name__)

# The Query protocol's version parameter. Pinned because a client sending a
# version this service does not implement should be told, not silently served
# whatever semantics happen to be current.
STS_API_VERSION = "2011-06-15"

# Actions this service answers. Kept as an explicit set so an unknown action is
# an `InvalidAction` rather than an `AttributeError` — a dispatcher that reaches
# for `getattr(self, params["Action"])` is one typo away from being an arbitrary
# method-call gadget.
STS_ACTIONS = frozenset({"AssumeRole", "GetCallerIdentity"})
IAM_ACTIONS = frozenset(
    {
        "CreateUser",
        "CreateRole",
        "CreateAccessKey",
        "PutUserPolicy",
        "PutRolePolicy",
        "DeleteUserPolicy",
        "UpdateAccessKey",
        "SimulatePrincipalPolicy",
    }
)


def get_state(request: Request) -> AppState:
    """Pull the assembled runtime off the app. Set by the lifespan in `main`."""
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]

public_router = APIRouter()
authz_router = APIRouter()


# --- the AWS-shaped API (:9025) ---------------------------------------------


@public_router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Unauthenticated on purpose — a load balancer has no credentials."""
    return {"status": "ok"}


async def _build_signed_request(request: Request) -> SignedRequest:
    """Capture the request exactly as it arrived, for the signature check.

    Nothing here normalizes anything. The signature covers the bytes the client
    sent, so any tidying done between the socket and the verifier is tidying that
    breaks every signature — a `.strip()` in the wrong place here costs an
    afternoon.
    """
    body = await request.body()
    query: dict[str, list[str]] = {}
    for key in request.query_params.keys():
        query[key] = request.query_params.getlist(key)
    return SignedRequest(
        method=request.method,
        path=request.url.path,
        query=query,
        headers=dict(request.headers),
        body=body,
        received_at=time.time(),
    )


async def _authenticate(request: Request, state: AppState) -> Identity:
    """Authenticate before anything else runs. V1's entry point.

    Two envelopes carry a SigV4 signature: the `Authorization` header, and a
    presigned URL's query parameters. Neither present means the caller did not
    even try, which is a different failure from trying and failing — and it is
    answered before any lookup, so an unauthenticated caller learns nothing about
    what exists here.
    """
    signed = await _build_signed_request(request)
    if "authorization" in request.headers:
        return await state.verifier.verify(signed)
    if "X-Amz-Signature" in request.query_params:
        return await state.verifier.verify_presigned(signed)
    raise MissingAuthenticationToken()


async def _query_parameters(request: Request) -> dict[str, str]:
    """Merge Query-protocol parameters from the query string and the form body.

    `GET` puts them in the query string, `POST` form-encodes them into the body.
    Both are the same protocol, and both are signed.
    """
    params: dict[str, str] = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        for key, value in form.items():
            if isinstance(value, str):
                params[key] = value
    return params


async def _dispatch(request: Request, state: AppState) -> dict[str, Any]:
    """The Query protocol's front door: authenticate, then route on `Action`."""
    identity = await _authenticate(request, state)

    params = await _query_parameters(request)
    action = params.get("Action")
    if not action:
        raise MissingAction()
    if action not in STS_ACTIONS and action not in IAM_ACTIONS:
        raise InvalidAction(f"unknown action {action!r}")

    # TODO(V1/V3): every IAM management action is itself an authorized call —
    # `CreateUser` requires `iam:CreateUser` on the caller. Right now
    # authentication happens and authorization does not, which means any valid
    # signature can do anything. Wiring the management plane through the
    # authorizer (once V3 and V5 exist) is what closes that, and it is the
    # difference between "we check who you are" and "we check what you may do".
    _ = identity

    # TODO(V4/V6): dispatch to the action handlers. `AssumeRole` and
    # `GetCallerIdentity` belong to V4; `SimulatePrincipalPolicy` to V6; the
    # management actions are store plumbing plus V2's `parse_policy` for
    # anything carrying a `PolicyDocument` parameter.
    #
    # Answer XML here, not JSON, if you take the Query-protocol horizontal box —
    # that is what makes `boto3` pointed at this endpoint work, and it is the
    # criterion's actual bar.
    raise NotImplementedError(f"V4/V6: dispatch the {action!r} action")


@public_router.get("/")
async def query_get(request: Request, state: StateDep) -> dict[str, Any]:
    """The Query protocol over GET — how presigned requests arrive."""
    return await _dispatch(request, state)


@public_router.post("/")
async def query_post(request: Request, state: StateDep) -> dict[str, Any]:
    """The Query protocol over POST — how every SDK sends it."""
    return await _dispatch(request, state)


# --- the authorization endpoint (:9026) -------------------------------------


class AuthorizeRequestBody(BaseModel):
    """What projects 23/24/06 send to ask a question.

    `context` is the interesting field. It is where the calling service reports
    the facts a condition might test — source IP, whether TLS was used, the time,
    a tag on the resource. A service that sends an empty context can never be
    protected by a conditional policy, so the completeness of this dict is a
    contract between this project and its callers, and it belongs in the
    versioned interface the SPEC asks for.
    """

    principal_arn: str
    action: str = Field(description="Service-qualified, e.g. `dynamodb:GetItem`")
    resource_arn: str
    context: dict[str, ContextValue] = {}
    # Which account owns the resource. Required rather than derived, because it
    # is what selects V3's same-account (OR) versus cross-account (AND) rule —
    # the single most consequential input to the decision, and one the calling
    # service knows for certain while this one would have to guess.
    resource_owner_account: str | None = None


class AuthorizeResponseBody(BaseModel):
    """The answer, and why.

    The `reason` and `deciding_*` fields are part of the contract, not debug
    output: a caller that only receives a boolean cannot tell its own user why
    they were refused, and the support ticket lands here instead.
    """

    decision: str
    allowed: bool
    reason: str = ""
    deciding_policy_type: str | None = None
    deciding_policy_id: str | None = None
    deciding_statement_id: str | None = None
    cached: bool = False


@authz_router.get("/healthz", include_in_schema=False)
async def authz_healthz() -> dict[str, str]:
    return {"status": "ok"}


@authz_router.post("/2025-01-01/authorize")
async def authorize(body: AuthorizeRequestBody, state: StateDep) -> AuthorizeResponseBody:
    """The hot path. Every request to projects 23/24/06 waits on this.

    The date-stamped path is the versioning the SPEC asks for: adding a policy
    type to the chain must not break a client written against today's shape.
    """
    # TODO(V2): `parse_arn` raises until V2 lands. Note this is the *only*
    # untrusted parsing on the hot path, which makes it the only place a hostile
    # caller can spend your CPU before any decision is made — bound it there.
    resource = parse_arn(body.resource_arn)
    principal_arn: Arn = parse_arn(body.principal_arn)

    # TODO(V5): assemble the PolicySet for this principal and resource from the
    # store — identity policies, the resource policy, the boundary, the session
    # policy from the token, and the account's SCPs — then hand it to the
    # authorizer. Assembling it is a store walk; deciding is V3's.
    #
    # TODO(V6): record the decision to the audit log after answering, not before,
    # and without awaiting the write.
    _ = (resource, principal_arn, state)
    raise NotImplementedError("V5: assemble the policy set and authorize")


@authz_router.post("/2025-01-01/simulate")
async def simulate(body: AuthorizeRequestBody, state: StateDep) -> AuthorizeResponseBody:
    """`SimulatePrincipalPolicy` — the same answer, uncached, without acting."""
    # TODO(V6): the parity criterion is the whole point. This must return the
    # same decision *and the same deciding statement* as `/authorize` for the
    # same inputs, which is why the simulator holds the evaluator rather than
    # reimplementing it.
    _ = (body, state)
    raise NotImplementedError("V6: simulate a decision against the live evaluator")


def result_to_body(result: Any) -> AuthorizeResponseBody:
    """Shape an `AuthorizationResult` for the wire. Plumbing.

    Kept separate so `/authorize` and `/simulate` cannot drift in how they
    report — two endpoints that format their own responses are two endpoints
    that will eventually disagree about what a deny looks like.
    """
    return AuthorizeResponseBody(
        decision=str(result.decision),
        allowed=result.is_allowed,
        reason=result.reason,
        deciding_policy_type=(
            str(result.deciding_policy_type) if result.deciding_policy_type else None
        ),
        deciding_policy_id=result.deciding_policy_id,
        deciding_statement_id=result.deciding_statement_id,
        cached=result.cached,
    )


def authorization_request(body: AuthorizeRequestBody, state: AppState) -> AuthorizationRequest:
    """Turn a wire body into the evaluator's input. Plumbing.

    Split out so the hot path and the simulator build *identical* requests — a
    simulator that assembles its context slightly differently is a simulator that
    answers a slightly different question, which is worse than not having one.
    """
    return AuthorizationRequest(
        principal=state.store.principal_for_user(state.store.get_user("root")),
        action=body.action,
        resource=parse_arn(body.resource_arn),
        context=dict(body.context),
    )
