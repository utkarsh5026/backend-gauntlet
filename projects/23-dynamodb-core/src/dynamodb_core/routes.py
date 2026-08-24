"""HTTP surface, shaped like DynamoDB's.

The real service exposes **one endpoint** and selects the operation with a target
header, rather than giving each operation its own path. That is unusual for HTTP
and worth keeping: it is why the AWS SDKs look the way they do, and mirroring it
means everything you learn here transfers to the real API.

Routing, request parsing and validation are wired. The operations themselves call
into the verticals, which raise until you build them — that is the worklist.

Wire format note: DynamoDB uses PascalCase JSON (`TableName`, `ConsistentRead`).
A pydantic alias generator maps that to snake_case Python once, here, so no other
module has to think about it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_pascal

from .errors import ValidationError
from .item import Item, KeySchema, key_of
from .state import AppState
from .table import TableDefinition

__all__ = ["public_router"]


class WireModel(BaseModel):
    """Accepts DynamoDB's PascalCase on the wire, snake_case in Python."""

    model_config = ConfigDict(alias_generator=to_pascal, populate_by_name=True)


class CreateTableRequest(WireModel):
    table_name: str
    partition_key: str
    sort_key: str | None = None
    read_capacity: int = 1000
    write_capacity: int = 1000


class PutItemRequest(WireModel):
    table_name: str
    item: Item
    condition_expression: str | None = None
    expression_attribute_names: dict[str, str] = {}
    expression_attribute_values: dict[str, Any] = {}


class KeyedRequest(WireModel):
    table_name: str
    key: Item
    consistent_read: bool = False


class QueryRequest(WireModel):
    table_name: str
    key_condition_expression: str | None = None
    expression_attribute_values: dict[str, Any] = {}
    index_name: str | None = None
    scan_index_forward: bool = True
    limit: int | None = None
    consistent_read: bool = False


def get_state(request: Request) -> AppState:
    """Pull the assembled runtime off the app. Set by the lifespan in `main`."""
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]
TargetHeader = Annotated[str | None, Header(alias="X-Target")]

public_router = APIRouter()


@public_router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@public_router.get("/tables")
async def list_tables(state: StateDep) -> dict[str, list[str]]:
    """Which tables this node serves. Plumbing, and the first thing you'll curl."""
    return {"TableNames": state.catalog.names()}


@public_router.post("/tables", status_code=201)
async def create_table(body: CreateTableRequest, state: StateDep) -> dict[str, str]:
    """Register a table. Plumbing — storage behaviour is V1's."""
    definition = TableDefinition(
        name=body.table_name,
        key_schema=KeySchema(partition_key=body.partition_key, sort_key=body.sort_key),
        read_capacity=body.read_capacity,
        write_capacity=body.write_capacity,
    )
    try:
        state.catalog.create_table(definition)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"TableName": definition.name}


@public_router.post("/")
async def data_plane(request: Request, state: StateDep, x_target: TargetHeader = None) -> Response:
    """The single data-plane endpoint; `X-Target` picks the operation.

    Every branch below is wired as far as the vertical it calls into. Running any
    of them today raises the `NotImplementedError` for the vertical that owns it.
    """
    if not x_target:
        raise ValidationError("missing X-Target header naming the operation")
    payload: dict[str, Any] = await request.json()

    match x_target:
        case "PutItem":
            body = PutItemRequest.model_validate(payload)
            context = state.catalog.get(body.table_name)
            # TODO(V3 -> V2 -> V5): the real write path is a sequence, and its
            # ORDER is the design: evaluate the condition, apply the write, update
            # every index, append the stream record — atomically, so a reader
            # never sees a half-applied write and a crash never splits them.
            old = context.table.put_item(body.item)
            return _json({"ConsumedCapacity": None, "Attributes": old})

        case "GetItem":
            body_k = KeyedRequest.model_validate(payload)
            context = state.catalog.get(body_k.table_name)
            item = context.table.get_item(_key(body_k.key, context.table.key_schema))
            return _json({"Item": item})

        case "DeleteItem":
            body_k = KeyedRequest.model_validate(payload)
            context = state.catalog.get(body_k.table_name)
            old = context.table.delete_item(_key(body_k.key, context.table.key_schema))
            return _json({"Attributes": old})

        case "UpdateItem":
            # TODO(V3): parse the UpdateExpression, evaluate any condition, apply.
            raise NotImplementedError("V3: UpdateItem — condition + update expression")

        case "Query":
            body_q = QueryRequest.model_validate(payload)
            context = state.catalog.get(body_q.table_name)
            # TODO(V1): parse KeyConditionExpression into the partition key value
            # and an optional SortKeyCondition. Reject a query with no partition
            # key — that is the rule this whole project is about.
            raise NotImplementedError("V1: parse KeyConditionExpression and query")

        case "Scan":
            body_s = QueryRequest.model_validate(payload)
            context = state.catalog.get(body_s.table_name)
            page = context.table.scan(limit=body_s.limit)
            return _json({"Items": page.items, "ScannedCount": page.scanned_count})

        case "TransactWriteItems":
            raise NotImplementedError("V3: TransactWriteItems — all-or-nothing across items")

        case _:
            raise ValidationError(f"unknown operation {x_target!r}")


@public_router.get("/streams/{table_name}")
async def read_stream(
    table_name: str, state: StateDep, iterator: str, limit: int = 100
) -> Response:
    """Read a batch of change records — the seam a consumer polls."""
    context = state.catalog.get(table_name)
    if context.stream is None:
        raise ValidationError(f"table {table_name!r} has no stream enabled")
    records, next_iterator = context.stream.read(iterator, limit=limit)
    return _json({"Records": records, "NextShardIterator": next_iterator})


def _key(raw: Item, schema: KeySchema):  # noqa: ANN202 - returns ItemKey
    """Turn a wire `Key` map into the table's key, or explain what's missing."""
    try:
        return key_of(raw, schema)
    except KeyError as exc:
        raise ValidationError(f"key is missing attribute {exc.args[0]!r}") from exc


def _json(payload: dict[str, Any]) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(content=jsonable(payload))


def jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop `None` values, the way the real API omits absent fields."""
    return {k: v for k, v in payload.items() if v is not None}
