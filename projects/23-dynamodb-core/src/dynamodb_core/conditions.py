"""V3 — Conditional writes & atomic updates.

Two callers read an item, both modify it, both write it back. One update is gone
and **nobody got an error**. That is the default behaviour of every store that
offers only "put this item", and it is why `ConditionExpression` exists: it turns a
blind write into a compare-and-set, which is the only concurrency primitive a
partitioned store can offer cheaply.

Three things live here:
  * **Conditions** — `attribute_not_exists(pk)`, `version = :v`, `size(x) < :n`,
    combined with AND/OR/NOT. Evaluated against the item as it exists *at write
    time*, atomically with the write itself. Evaluate it a moment earlier and you
    have reintroduced the race you were trying to close.
  * **Updates** — `SET`/`REMOVE`/`ADD` applied server-side. `ADD` on a number is
    what makes an atomic counter atomic: the increment never leaves the server, so
    there is no read-modify-write window for anyone to interleave into.
  * **Transactions** — all-or-nothing across several items.

Scaffold state: the expression types are modelled; parsing, evaluation and the
transaction protocol raise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .item import AttributeValue, Item, ItemKey, TableName

__all__ = [
    "ConditionExpression",
    "TransactItem",
    "TransactOperation",
    "UpdateExpression",
    "apply_update",
    "evaluate_condition",
]


@dataclass(slots=True)
class ConditionExpression:
    """A condition, exactly as it arrives on the wire.

    DynamoDB keeps the expression text separate from its names and values so that
    an attribute called `size` or `status` (both reserved words) can be referenced
    as `#s`, and so a value can never be spliced into the expression as text. That
    separation is the same defence a parameterised SQL query gives you — worth
    naming as such in the design doc.
    """

    expression: str
    names: dict[str, str] = field(default_factory=dict[str, str])
    values: dict[str, AttributeValue] = field(default_factory=dict[str, AttributeValue])


@dataclass(slots=True)
class UpdateExpression:
    """A server-side mutation, e.g. `SET a = :v REMOVE b ADD hits :one`."""

    expression: str
    names: dict[str, str] = field(default_factory=dict[str, str])
    values: dict[str, AttributeValue] = field(default_factory=dict[str, AttributeValue])


class TransactOperation(StrEnum):
    PUT = "Put"
    UPDATE = "Update"
    DELETE = "Delete"
    # Not a write at all: asserts a condition on an item this transaction reads
    # but does not modify. Without it you cannot make a decision about item A
    # depend on the state of item B.
    CONDITION_CHECK = "ConditionCheck"


@dataclass(slots=True)
class TransactItem:
    """One leg of a transaction."""

    operation: TransactOperation
    table: TableName
    key: ItemKey
    item: Item | None = None
    update: UpdateExpression | None = None
    condition: ConditionExpression | None = None


def evaluate_condition(condition: ConditionExpression, item: Item | None) -> bool:
    """Is this condition true of `item` right now? (`None` = the item is absent.)"""
    # TODO(V3): parse, then evaluate. The grammar is small enough to hand-write a
    # recursive-descent parser for, and doing so is most of the value here:
    #
    #   operand   := path | :value | function
    #   function  := attribute_exists(path) | attribute_not_exists(path)
    #              | attribute_type(path, :t) | begins_with(path, :v)
    #              | contains(path, :v) | size(path)
    #   comparison:= operand (= | <> | < | <= | > | >=) operand
    #              | operand BETWEEN operand AND operand
    #              | operand IN (operand, ...)
    #   condition := comparison | NOT condition | condition AND condition
    #              | condition OR condition | ( condition )
    #
    # Tokenize first, keep the parser separate from the evaluator, and resolve #n
    # names and :v values ONLY at evaluation — never by string substitution into
    # the expression, which is how you would reinvent injection.
    #
    # A path may be nested: `a.b[0].c`. Missing intermediate segments make the
    # operand absent, which is not the same as false.
    raise NotImplementedError("V3: parse and evaluate a condition expression")


def apply_update(update: UpdateExpression, item: Item | None) -> Item:
    """Apply an UpdateExpression, returning the new item.

    Called only after any condition has already passed, and atomically with the
    write — the caller owns that ordering.
    """
    # TODO(V3): support the clauses that matter:
    #   SET    a = :v, b = a + :n, c = if_not_exists(c, :v), d = list_append(d, :v)
    #   REMOVE a, b[2]
    #   ADD    counter :n        (number add, or set union for SS/NS/BS)
    #   DELETE tags :subset      (set difference)
    #
    # ADD is the one that makes an atomic counter work: the read and the write
    # both happen here, server-side, so no two callers can interleave. Use
    # `decimal.Decimal` for the arithmetic — `float` will silently corrupt a
    # counter once it passes 2^53.
    #
    # An update to a non-existent item CREATES it (with its key attributes), which
    # surprises people — make sure your tests pin that behaviour.
    raise NotImplementedError("V3: apply SET / REMOVE / ADD / DELETE to an item")


def check_transaction(items: list[TransactItem]) -> None:
    """Validate a transaction before any of it is applied.

    DynamoDB rejects a transaction that touches the same item twice, because the
    result would depend on an ordering it never promised.
    """
    # TODO(V3): reject duplicate (table, key) pairs and over-large transactions.
    # Cheap, and it removes a whole class of "why did only one of my two updates
    # land" questions.
    raise NotImplementedError("V3: validate a transaction's legs before applying any")
