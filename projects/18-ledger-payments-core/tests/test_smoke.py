"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1–V4; those are yours, and each
vertical's "Proof" line names what it has to demonstrate. What is here is the plumbing:
the app boots, every route is reachable, the framework's validation answers (rule zero
included), errors map to the right statuses without leaking, config stays in lockstep
with compose, the dispatcher loop and shutdown behave, and the unbuilt parts raise.

That last group is the worklist made executable. When you implement a vertical, its
tests here are the first thing that should fail — delete them then.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError

from ledger_payments_core import auth, idempotency, isolation, ledger, routes, webhooks
from ledger_payments_core.config import Settings
from ledger_payments_core.db import MIGRATIONS_DIR, Conn
from ledger_payments_core.errors import (
    AppError,
    BadRequestError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    InsufficientFundsError,
    NotFoundError,
    RetriesExhaustedError,
    UnauthorizedError,
    install_error_handlers,
)
from ledger_payments_core.idempotency import StoredResponse
from ledger_payments_core.ledger import EntryDraft, TransactionDraft
from ledger_payments_core.main import (
    build_state,
    create_app,
    create_redis,
    stop_background,
    webhook_config,
)
from ledger_payments_core.models import NewAccount, NewTransfer
from ledger_payments_core.state import AppState, task_failure, wait_for_shutdown

PROJECT_DIR = Path(__file__).resolve().parents[1]

GRADED_SERIES = (
    "ledger_transfers_total",
    "ledger_serialization_retries_total",
    "ledger_idempotency_lookups_total",
    "ledger_webhook_deliveries_total",
    "ledger_webhook_outbox_lag_seconds",
    "ledger_transfer_duration_seconds",
)


def _transfer_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "from": str(uuid4()),
        "to": str(uuid4()),
        "amount": 1000,
        "currency": "USD",
    }
    body.update(overrides)
    return body


def _env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (PROJECT_DIR / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.split("#", 1)[0].strip()
    return values


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


async def test_healthz_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


async def test_metrics_render_every_graded_series(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for series in GRADED_SERIES:
        assert series in response.text, series
    # Pre-initialised, so the DLQ series exists at zero before anything dies.
    assert 'ledger_webhook_deliveries_total{state="dead"} 0.0' in response.text


async def test_create_account_reaches_v1(client: httpx.AsyncClient) -> None:
    response = await client.post("/accounts", json={"name": "alice", "currency": "USD"})
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V1:")


async def test_balance_reaches_v1(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/accounts/{uuid4()}/balance")
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V1:")


async def test_get_transaction_reaches_v1(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/transactions/{uuid4()}")
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V1:")


async def test_transfer_without_a_key_reaches_v2(client: httpx.AsyncClient) -> None:
    response = await client.post("/transfers", json=_transfer_body())
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V2:")


async def test_transfer_with_a_key_reaches_v3(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/transfers", json=_transfer_body(), headers={"Idempotency-Key": "order-1"}
    )
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V3:")


async def test_the_fingerprint_sees_the_exact_request_bytes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V3 is handed what the client sent — not a re-serialization of the parsed model."""
    seen: list[bytes] = []

    def spy(body: bytes) -> str:
        seen.append(body)
        raise NotImplementedError("V3: spy")

    monkeypatch.setattr(routes, "fingerprint", spy)
    raw = (
        f'{{"to": "{uuid4()}",   "from": "{uuid4()}", "currency": "USD", "amount": 1000}}'
    ).encode()
    response = await client.post(
        "/transfers",
        content=raw,
        headers={"content-type": "application/json", "idempotency-key": "order-2"},
    )
    assert response.status_code == 501
    assert seen == [raw]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_transfer_body(amount=10.5), id="fractional_amount"),
        pytest.param(_transfer_body(amount=1000.0), id="integral_float_amount"),
        pytest.param(_transfer_body(amount="1000"), id="numeric_string_amount"),
        pytest.param(_transfer_body(amount=True), id="boolean_amount"),
        pytest.param({k: v for k, v in _transfer_body().items() if k != "to"}, id="missing_to"),
        pytest.param(_transfer_body(**{"from": "not-a-uuid"}), id="malformed_uuid"),
        pytest.param(_transfer_body(memo="hi"), id="unknown_field"),
    ],
)
async def test_transfer_bodies_are_validated_before_money_moves(
    client: httpx.AsyncClient, body: dict[str, object]
) -> None:
    response = await client.post("/transfers", json=body)
    assert response.status_code == 422


@pytest.mark.parametrize("key", [pytest.param("", id="empty"), pytest.param("k" * 256, id="huge")])
async def test_idempotency_keys_are_bounded(client: httpx.AsyncClient, key: str) -> None:
    response = await client.post(
        "/transfers", json=_transfer_body(), headers={"Idempotency-Key": key}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/accounts/not-a-uuid/balance", "/transactions/not-a-uuid"])
async def test_ids_must_be_uuids(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 422


async def test_account_bodies_are_validated(client: httpx.AsyncClient) -> None:
    response = await client.post("/accounts", json={"name": "alice"})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


def test_transfers_read_from_and_to_by_their_json_names() -> None:
    transfer = NewTransfer.model_validate(_transfer_body(amount=1))
    assert isinstance(transfer.from_account, UUID)
    assert isinstance(transfer.to_account, UUID)
    assert {"from", "to"} <= set(transfer.model_dump(by_alias=True))


def test_accounts_default_to_no_overdraft() -> None:
    assert NewAccount(name="alice", currency="USD").allow_negative is False


@pytest.mark.parametrize(
    ("amounts", "net"),
    [((), 0), ((-1000, 1000), 0), ((-1000, 999), -1), ((5, 5, -10), 0)],
)
def test_a_drafts_net_is_the_signed_sum(amounts: tuple[int, ...], net: int) -> None:
    entries = tuple(EntryDraft(account_id=uuid4(), amount=a, currency="USD") for a in amounts)
    assert TransactionDraft(entries=entries).net == net


# --------------------------------------------------------------------------- #
# The worklist: every unbuilt piece raises its todo
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("prefix", "call"),
    [
        pytest.param(
            "V1:",
            lambda: ledger.draft_transfer(NewTransfer.model_validate(_transfer_body())),
            id="draft_transfer",
        ),
        pytest.param(
            "V2:", lambda: isolation.is_serialization_conflict(RuntimeError()), id="is_conflict"
        ),
        pytest.param("V3:", lambda: idempotency.fingerprint(b"{}"), id="fingerprint"),
        pytest.param("V4:", lambda: webhooks.sign(b"secret", 0, b"{}"), id="sign"),
        pytest.param("V4:", lambda: webhooks.verify(b"secret", 0, b"{}", "00"), id="verify"),
        pytest.param("V4:", lambda: webhooks.backoff(1), id="backoff"),
    ],
)
def test_unbuilt_pure_functions_raise_their_todo(prefix: str, call: Callable[[], object]) -> None:
    with pytest.raises(NotImplementedError, match=f"^{re.escape(prefix)}"):
        call()


async def _never_executes() -> StoredResponse:
    raise AssertionError("executed before V3 exists")


async def test_unbuilt_io_raises_its_todo(state: AppState, settings: Settings) -> None:
    fake_conn = cast(Conn, object())
    draft = TransactionDraft(entries=())
    new_transfer = NewTransfer.model_validate(_transfer_body())
    async with httpx.AsyncClient() as http:
        calls: list[tuple[str, Awaitable[object]]] = [
            ("V1:", state.ledger.create_account(NewAccount(name="alice", currency="USD"))),
            ("V1:", state.ledger.get_account(uuid4())),
            ("V1:", state.ledger.post(draft)),
            ("V1:", ledger.post_on(fake_conn, draft)),
            ("V1:", state.ledger.balance(uuid4())),
            ("V1:", state.ledger.get_transaction(uuid4())),
            ("V2:", isolation.transfer(state.ledger, state.policy, new_transfer)),
            ("V3:", state.idempotency.run("order-3", "hash", _never_executes)),
            (
                "V4:",
                webhooks.enqueue_settled(
                    fake_conn, endpoint_url="http://x", transaction_id=uuid4(), payload={}
                ),
            ),
            ("V4:", webhooks.dispatch_once(state.pool, http, webhook_config(settings))),
            ("security:", auth.require_api_key(state, "Bearer dev-key-change-me")),
        ]
        for prefix, call in calls:
            with pytest.raises(NotImplementedError, match=f"^{re.escape(prefix)}"):
                await call


# --------------------------------------------------------------------------- #
# Errors → HTTP
# --------------------------------------------------------------------------- #


def _raising_app(error: Exception) -> FastAPI:
    async def boom() -> None:
        raise error

    app = FastAPI()
    install_error_handlers(app)
    app.add_api_route("/boom", boom)
    return app


async def _get_boom(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ledger.test") as http:
        return await http.get("/boom")


@pytest.mark.parametrize(
    ("error", "status"),
    [
        pytest.param(NotFoundError(), 404, id="not_found"),
        pytest.param(UnauthorizedError(), 401, id="unauthorized"),
        pytest.param(BadRequestError("amount must be positive"), 400, id="bad_request"),
        pytest.param(InsufficientFundsError(), 402, id="insufficient_funds"),
        pytest.param(IdempotencyConflictError(), 409, id="key_conflict"),
        pytest.param(IdempotencyInProgressError(), 409, id="key_in_progress"),
        pytest.param(RetriesExhaustedError(), 409, id="retries_exhausted"),
    ],
)
async def test_client_errors_render_their_status_and_message(error: AppError, status: int) -> None:
    response = await _get_boom(_raising_app(error))
    assert response.status_code == status
    assert response.json() == {"error": error.message}


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(AppError("ledger internals"), id="app_error"),
        pytest.param(asyncpg.PostgresError("violates constraint entries_pkey"), id="postgres"),
        pytest.param(RedisConnectionError("redis://:hunter2@cache:6379"), id="redis"),
    ],
)
async def test_server_errors_never_leak_their_message(error: Exception) -> None:
    response = await _get_boom(_raising_app(error))
    assert response.status_code == 500
    assert response.json() == {"error": "internal server error"}
    assert "hunter2" not in response.text


# --------------------------------------------------------------------------- #
# Config, secrets, and the schema
# --------------------------------------------------------------------------- #


def test_env_example_and_settings_name_the_same_variables() -> None:
    documented = set(_env_example())
    fields = {name.upper() for name in Settings.model_fields}
    assert documented == fields, {
        "undocumented": fields - documented,
        "unknown": documented - fields,
    }


def test_code_fallbacks_match_compose_and_env_example() -> None:
    compose = (PROJECT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    env = _env_example()
    assert '"5418:5432"' in compose
    assert '"6318:6379"' in compose
    assert Settings.model_fields["database_url"].default == env["DATABASE_URL"]
    assert Settings.model_fields["redis_url"].default == env["REDIS_URL"]


def test_secrets_are_masked_wherever_they_are_printed() -> None:
    settings = Settings(
        api_keys=SecretStr("sk_live_old,sk_live_new"),
        webhook_signing_secret=SecretStr("whsec_topsecret"),
    )
    printed = repr(settings) + str(settings) + repr(webhook_config(settings))
    assert "sk_live" not in printed
    assert "topsecret" not in printed


def test_api_keys_split_for_rotation() -> None:
    settings = Settings(api_keys=SecretStr(" old , new,, "))
    assert [key.get_secret_value() for key in settings.api_key_list] == ["old", "new"]


def test_migrations_create_the_five_tables() -> None:
    sql = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
    for table in ("accounts", "transactions", "entries", "idempotency_keys", "webhook_outbox"):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\b", sql), table


# --------------------------------------------------------------------------- #
# The dispatcher and shutdown
# --------------------------------------------------------------------------- #


async def test_the_dispatcher_is_off_by_default(app: FastAPI, state: AppState) -> None:
    assert state.background == []


async def test_an_enabled_dispatcher_dies_loudly_at_v4(settings: Settings) -> None:
    enabled = settings.model_copy(update={"run_dispatcher": True})
    pool: asyncpg.Pool[asyncpg.Record] = asyncpg.create_pool(
        dsn=enabled.database_url, min_size=1, max_size=2
    )
    client = create_redis(enabled)
    state = build_state(enabled, pool=pool, redis=client)
    application = create_app(state=state)
    try:
        async with application.router.lifespan_context(application):
            assert len(state.background) == 1
            done, _ = await asyncio.wait(state.background, timeout=2)
            assert done, "the dispatcher should reach its first round immediately"
            failure = task_failure(state.background[0])
            assert isinstance(failure, NotImplementedError)
            assert str(failure).startswith("V4:")
    finally:
        await client.aclose()


async def test_the_dispatch_loop_settles_its_round_before_stopping(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()
    rounds: list[str] = []

    async def slow_round(_pool: object, _http: object, _cfg: webhooks.WebhookConfig) -> int:
        rounds.append("start")
        started.set()
        await release.wait()
        rounds.append("end")
        return 0

    monkeypatch.setattr(webhooks, "dispatch_once", slow_round)
    loop = asyncio.create_task(
        webhooks.dispatch_loop(
            cast("asyncpg.Pool[asyncpg.Record]", None),
            cast(httpx.AsyncClient, None),
            webhook_config(settings),
            shutdown,
        )
    )
    await started.wait()
    shutdown.set()
    await asyncio.sleep(0.01)
    assert not loop.done(), "shutdown must not abandon a round in flight"
    release.set()
    await asyncio.wait_for(loop, timeout=1)
    assert rounds == ["start", "end"]


async def test_stop_background_drains_a_task_that_honours_shutdown(state: AppState) -> None:
    finished = asyncio.Event()

    async def polite() -> None:
        await state.shutdown.wait()
        finished.set()

    state.background.append(asyncio.create_task(polite()))
    await stop_background(state, drain_secs=1.0)
    assert finished.is_set()


async def test_stop_background_cancels_what_outlives_the_drain_budget(state: AppState) -> None:
    async def stubborn() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(stubborn())
    state.background.append(task)
    await stop_background(state, drain_secs=0.05)
    assert task.cancelled()


async def test_wait_for_shutdown_wakes_early() -> None:
    event = asyncio.Event()
    asyncio.get_running_loop().call_later(0.01, event.set)
    assert await wait_for_shutdown(event, timeout=5) is True


async def test_wait_for_shutdown_times_out() -> None:
    assert await wait_for_shutdown(asyncio.Event(), timeout=0.01) is False
