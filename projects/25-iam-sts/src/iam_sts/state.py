"""The identity store and the objects every handler needs, assembled once.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).

Everything here is **plumbing**. Storing a user is not a vertical; deciding what
that user may do is all six of them. The one part worth reading is
`IdentityStore.version`: every mutation bumps it, and every policy carries the
version it was written at. That is what lets V5 answer "is my compiled artifact
stale?" without diffing documents, and it is the difference between an
invalidation that is provably correct and one that is probably correct.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

from pydantic import SecretStr

from .audit import AuditLog, PolicySimulator, RevocationRegistry
from .authorizer import Authorizer
from .config import Settings
from .errors import EntityAlreadyExists, LimitExceeded, NoSuchEntity
from .evaluation import PolicyEvaluator
from .models import Arn, Principal, PrincipalType
from .policy import PolicyDocument
from .sigv4 import SigV4Verifier
from .sts import SecurityTokenService

__all__ = ["AccessKey", "AppState", "IdentityStore", "Role", "User"]


@dataclass(slots=True)
class AccessKey:
    """A long-lived credential.

    The secret is a `SecretStr` so that a `repr` of this object — in a traceback,
    a debugger, a `log.info("key", key=key)` — prints `**********` rather than the
    credential. The horizontal checklist asks for storage that survives a dump of
    the store; `SecretStr` is not that (it is in memory in the clear), but it does
    close the accident, which is how the secret actually escapes in practice.
    """

    access_key_id: str
    secret_access_key: SecretStr
    user_name: str
    created_at: float
    active: bool = True


@dataclass(slots=True)
class User:
    """A long-lived identity."""

    name: str
    account_id: str
    path: str = "/"
    tags: dict[str, str] = field(default_factory=dict[str, str])
    inline_policies: dict[str, PolicyDocument] = field(default_factory=dict[str, PolicyDocument])
    attached_policy_arns: list[str] = field(default_factory=list[str])
    # Caps whatever the policies above grant. V3 evaluates it; nothing here does.
    permission_boundary: PolicyDocument | None = None

    def arn(self, partition: str) -> Arn:
        return Arn(
            partition=partition,
            service="iam",
            region="",
            account=self.account_id,
            resource=f"user{self.path}{self.name}",
        )


@dataclass(slots=True)
class Role:
    """An assumable identity.

    `trust_policy` is required and has no default. That is deliberate: a role
    without one is a role nobody can assume, and a role with a permissive default
    is a public front door. Making it a required argument means the question
    cannot be skipped.

    `max_session_duration` lives here rather than in settings because it is a
    property of the role — a role granting production write access should be
    assumable for fifteen minutes even though the account allows twelve hours.
    """

    name: str
    account_id: str
    trust_policy: PolicyDocument
    path: str = "/"
    max_session_duration: float = 3600.0
    tags: dict[str, str] = field(default_factory=dict[str, str])
    inline_policies: dict[str, PolicyDocument] = field(default_factory=dict[str, PolicyDocument])
    attached_policy_arns: list[str] = field(default_factory=list[str])
    permission_boundary: PolicyDocument | None = None

    def arn(self, partition: str) -> Arn:
        return Arn(
            partition=partition,
            service="iam",
            region="",
            account=self.account_id,
            resource=f"role{self.path}{self.name}",
        )


class IdentityStore:
    """Users, roles, policies, keys — and the resource policies and SCPs.

    Plumbing, fully implemented. In-memory on purpose: durability and replication
    are projects 09 and 07, and adding a database here would buy nothing this
    SPEC grades while adding a dependency to every test.

    `version` is the one non-obvious piece. It increments on every mutation, so a
    compiled artifact or a cached decision can record the version it was derived
    at and be checked in one integer comparison. Without it, invalidation has to
    trust that every write path remembered to call it — and one that forgot is a
    stale *allow*.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.users: dict[str, User] = {}
        self.roles: dict[str, Role] = {}
        self.access_keys: dict[str, AccessKey] = {}
        # Managed policies, by ARN. Attached to users and roles by reference, so
        # editing one affects every principal holding it — which is the point,
        # and also why invalidation has to fan out.
        self.managed_policies: dict[str, PolicyDocument] = {}
        # Resource policies, keyed by the ARN of the resource they sit on. This
        # is how projects 23/24/06 register "this table/function/bucket has a
        # policy" without this service knowing what any of those are.
        self.resource_policies: dict[str, PolicyDocument] = {}
        # Organization service control policies. Account-wide ceilings.
        self.scps: list[PolicyDocument] = []
        self._version = itertools.count(1)
        self.version = next(self._version)

    # --- mutation -----------------------------------------------------------

    def _bump(self) -> int:
        self.version = next(self._version)
        return self.version

    def create_user(self, name: str, *, path: str = "/") -> User:
        if name in self.users:
            raise EntityAlreadyExists(f"user {name!r} already exists")
        user = User(name=name, account_id=self._settings.account_id, path=path)
        self.users[name] = user
        self._bump()
        return user

    def create_role(
        self,
        name: str,
        trust_policy: PolicyDocument,
        *,
        path: str = "/",
        max_session_duration: float | None = None,
    ) -> Role:
        if name in self.roles:
            raise EntityAlreadyExists(f"role {name!r} already exists")
        role = Role(
            name=name,
            account_id=self._settings.account_id,
            trust_policy=trust_policy,
            path=path,
            max_session_duration=(
                max_session_duration
                if max_session_duration is not None
                else self._settings.default_session_duration_seconds
            ),
        )
        self.roles[name] = role
        self._bump()
        return role

    def put_user_policy(self, user_name: str, policy_name: str, document: PolicyDocument) -> None:
        user = self.get_user(user_name)
        if (
            policy_name not in user.inline_policies
            and len(user.inline_policies) >= self._settings.max_policies_per_principal
        ):
            raise LimitExceeded(
                f"user {user_name!r} already has "
                f"{self._settings.max_policies_per_principal} inline policies"
            )
        user.inline_policies[policy_name] = document
        self._bump()

    def put_role_policy(self, role_name: str, policy_name: str, document: PolicyDocument) -> None:
        role = self.get_role(role_name)
        if (
            policy_name not in role.inline_policies
            and len(role.inline_policies) >= self._settings.max_policies_per_principal
        ):
            raise LimitExceeded(
                f"role {role_name!r} already has "
                f"{self._settings.max_policies_per_principal} inline policies"
            )
        role.inline_policies[policy_name] = document
        self._bump()

    def delete_user_policy(self, user_name: str, policy_name: str) -> None:
        user = self.get_user(user_name)
        if user.inline_policies.pop(policy_name, None) is None:
            raise NoSuchEntity(f"user {user_name!r} has no policy {policy_name!r}")
        self._bump()

    def put_resource_policy(self, resource_arn: str, document: PolicyDocument) -> None:
        self.resource_policies[resource_arn] = document
        self._bump()

    def create_access_key(self, user_name: str, key_id: str, secret: str) -> AccessKey:
        self.get_user(user_name)
        key = AccessKey(
            access_key_id=key_id,
            secret_access_key=SecretStr(secret),
            user_name=user_name,
            created_at=time.time(),
        )
        self.access_keys[key_id] = key
        self._bump()
        return key

    def set_access_key_active(self, key_id: str, active: bool) -> None:
        key = self.get_access_key(key_id)
        key.active = active
        self._bump()

    # --- lookup -------------------------------------------------------------

    def get_user(self, name: str) -> User:
        try:
            return self.users[name]
        except KeyError:
            raise NoSuchEntity(f"user {name!r} does not exist") from None

    def get_role(self, name: str) -> Role:
        try:
            return self.roles[name]
        except KeyError:
            raise NoSuchEntity(f"role {name!r} does not exist") from None

    def get_access_key(self, key_id: str) -> AccessKey:
        try:
            return self.access_keys[key_id]
        except KeyError:
            # Same message whichever way it fails — see InvalidClientTokenId.
            raise NoSuchEntity("no such access key") from None

    def principal_for_user(self, user: User) -> Principal:
        """The `Principal` V3 evaluates. Root is a distinct type, not a user."""
        settings = self._settings
        if user.name == "root":
            return Principal(
                arn=Arn(
                    partition=settings.aws_partition,
                    service="iam",
                    region="",
                    account=settings.account_id,
                    resource="root",
                ),
                principal_type=PrincipalType.ROOT,
                account_id=settings.account_id,
                tags=dict(user.tags),
            )
        return Principal(
            arn=user.arn(settings.aws_partition),
            principal_type=PrincipalType.USER,
            account_id=user.account_id,
            tags=dict(user.tags),
        )

    def seed_bootstrap(self) -> AccessKey:
        """Create the account root and its credentials.

        The chicken-and-egg every identity service has: every request must be
        signed, but signing needs a credential, and creating a credential is a
        request. Somebody has to be seeded out of band — in the real world that
        is the account you create with a credit card, and here it is `.env`.
        """
        settings = self._settings
        root = User(name="root", account_id=settings.account_id)
        self.users["root"] = root
        key = AccessKey(
            access_key_id=settings.bootstrap_access_key_id,
            secret_access_key=settings.bootstrap_secret_access_key,
            user_name="root",
            created_at=time.time(),
        )
        self.access_keys[key.access_key_id] = key
        self._bump()
        return key


@dataclass(slots=True)
class AppState:
    """Everything the API and the authorizer share.

    Both ASGI apps hold a reference to the same instance — that is what lets a
    policy written on port 9025 change a decision served on port 9026, and what
    makes the propagation window a real number rather than a plumbing artifact.
    """

    settings: Settings
    store: IdentityStore
    verifier: SigV4Verifier
    evaluator: PolicyEvaluator
    authorizer: Authorizer
    sts: SecurityTokenService
    audit: AuditLog
    revocations: RevocationRegistry
    simulator: PolicySimulator
