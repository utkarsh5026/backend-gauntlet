"""ARNs — the name every AWS service passes to every other AWS service.

`arn:aws:sqs:us-east-1:123456789012:orders.fifo` is six colon-separated fields,
and the last one is whatever the service wanted. That is the whole format, and it
is worth having in one place because the tier passes ARNs across service
boundaries: 29's redrive names a source queue by ARN, 24's event source mapping
names 23's stream by ARN, and 25's policies name every one of them.

The fields are not decoration. **Account and region are in the name**, which is
what makes an ARN globally unique and what makes cross-account access expressible
at all — a policy that says "this queue" is really saying "this queue, in this
account, in this region", and the confused-deputy problems in project 25 are
exactly what happens when a service checks the resource and forgets the account.

Deliberately **not** here: wildcard matching (`arn:aws:s3:::bucket/*`). That is
the heart of project 25's V2 policy language, where the interesting cases live —
`*` versus `?`, whether `*` crosses a `/`, how `Not*` inverts, and how a variable
interpolates into the pattern before matching. Parsing is plumbing; matching is
the vertical.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidParameterValue

__all__ = ["Arn"]

_FIELDS = 6


@dataclass(frozen=True, slots=True)
class Arn:
    """A parsed ARN. Frozen because an ARN is an identity, not a builder."""

    partition: str
    service: str
    region: str
    account_id: str
    resource: str

    @classmethod
    def parse(cls, value: str) -> Arn:
        """Parse an ARN, or raise `InvalidParameterValue`.

        Split with `maxsplit` so a resource containing colons — which is legal,
        and common (`function:my-fn:PROD`, `table/orders/stream/2026-08-31`) —
        survives intact rather than being torn into fields that do not exist.
        """
        parts = value.split(":", _FIELDS - 1)
        if len(parts) != _FIELDS or parts[0] != "arn":
            raise InvalidParameterValue(f"{value!r} is not a valid ARN")
        _, partition, service, region, account_id, resource = parts
        if not partition or not service or not resource:
            raise InvalidParameterValue(f"{value!r} is missing a required ARN field")
        return cls(
            partition=partition,
            service=service,
            region=region,
            account_id=account_id,
            resource=resource,
        )

    def __str__(self) -> str:
        return ":".join(
            ["arn", self.partition, self.service, self.region, self.account_id, self.resource]
        )

    @property
    def resource_type(self) -> str | None:
        """The part before the first `/` or `:` in the resource, when there is one.

        Services disagree on the separator — S3 uses none, Lambda uses `:`,
        DynamoDB uses `/` — so both are accepted here and neither is normalized
        away. A resource with no separator (an S3 bucket) has no type.
        """
        for separator in ("/", ":"):
            head, found, _ = self.resource.partition(separator)
            if found:
                return head
        return None

    @property
    def resource_id(self) -> str:
        """The resource without its type prefix — the bare name."""
        for separator in ("/", ":"):
            _, found, tail = self.resource.partition(separator)
            if found:
                return tail
        return self.resource

    def is_same_account(self, account_id: str) -> bool:
        """Whether this ARN belongs to `account_id`.

        A one-line method because it is the check that is easiest to forget and
        most expensive to omit: an authorizer that matches the resource and skips
        the account has just granted a stranger's queue to your caller.
        """
        return self.account_id == account_id
