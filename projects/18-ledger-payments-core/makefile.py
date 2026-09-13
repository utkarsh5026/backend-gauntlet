#!/usr/bin/env python3
"""ledger-payments-core — local dev task runner.

A wrapper around the day-to-day commands for this project: uv, the docker-compose
Postgres + Redis, migrations, and probes that make each vertical visible. The
`Makefile` shells out to this file so there is one source of truth with colors,
emojis and readable output. Help tables use `tools/makefile_help.py`.

The probes follow the SPEC's suggested order of attack:

* `make account`  — V1: `POST /accounts` (NAME=, CURRENCY=, OVERDRAFT=1).
* `make transfer` — V2: `POST /transfers` (FROM=, TO=, AMOUNT=). Add KEY= to send an
                    `Idempotency-Key` for V3 — then run it twice and compare.
* `make balance`  — V1's derived read (ACCOUNT=).
* `make audit`    — Σ of every entry in the ledger, straight from Postgres. Anything but
                    0 means money was created or destroyed.
* `make receiver` — a local webhook sink for V4 (FAIL=1 answers 500 every time, so you
                    can watch backoff walk an event to the DLQ).

On the bare scaffold every money route answers `501` with the todo that blocks it,
which is the fastest way to see which vertical you are on.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    C,
    make_runner,
    register_compose_lifecycle,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
)

runner = make_runner(
    crate="ledger-payments-core",
    help_title="💸 ledger-payments-core (double-entry · isolation · idempotency · webhooks)",
    project_dir=PROJECT_DIR,
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make dev"),
        ("Open accounts (V1)", "make account NAME=alice && make account NAME=bob"),
        ("Move money (V2/V3)", "make transfer FROM=<id> TO=<id> AMOUNT=1000 KEY=order-1"),
        ("Is money conserved?", "make audit"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_compose_lifecycle(runner)
register_python_run(runner)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


def _env() -> dict[str, str]:
    return runner.load_dotenv()


def _url(path: str = "") -> str:
    port = _env().get("PORT", runner.config.default_port)
    return f"http://localhost:{port}{path}"


def _api_key() -> str:
    """The first configured key. Sent on every probe, so they keep working once auth
    is wired — never printed."""
    return _env().get("API_KEYS", "dev-key-change-me").split(",")[0].strip()


def _require_env(name: str, hint: str) -> str:
    value = os.environ.get(name)
    if not value:
        runner.fail(f"set {name}=<…> — {hint}")
        sys.exit(1)
    return value


def _request(
    url: str, *, body: object | None = None, headers: dict[str, str] | None = None
) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("authorization", f"Bearer {_api_key()}")
    if data is not None:
        request.add_header("content-type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def _report(code: int, body: str) -> None:
    if code == 0:
        runner.fail(f"no answer — is the server running? ({body})")
        sys.exit(1)
    color = C.GREEN if code < 400 else C.YELLOW if code == 501 else C.RED
    print(f"  {color}{code}{C.RESET}  {body.strip()}")
    if code == 501:
        print(f"   {C.DIM}that todo is the worklist{C.RESET}")
    print()


def _psql(sql: str) -> None:
    runner.run(
        [
            *runner.compose,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "ledger",
            "-d",
            "ledger",
            "-c",
            sql,
        ],
        cwd=runner.project_dir,
        check=False,
    )


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


@runner.task("up", "🐳", "Services", "Start postgres + redis and wait until healthy")
def up() -> None:
    runner.step("🐳", "starting postgres + redis…")
    # `--wait` blocks on the compose healthchecks, so this returns only once both
    # actually accept connections — not merely once their containers started.
    runner.run([*runner.compose, "up", "-d", "--wait"], cwd=runner.project_dir)
    runner.ok("healthy → postgres :5418 · redis :6318")


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("migrate", "🗃️", "Services", "Apply SQL migrations")
def migrate() -> None:
    """The Python answer to `sqlx migrate run` — see `ledger_payments_core.migrate`."""
    runner.step("🗃️", "applying migrations…")
    runner.uv(
        "run",
        "python",
        "-m",
        "ledger_payments_core.migrate",
        str(runner.project_dir / "migrations"),
        env=_env(),
    )
    runner.ok("migrations applied")


@runner.task("reset-db", "💥", "Services", "Drop volumes, recreate, migrate (destructive)")
def reset_db() -> None:
    runner.warn("dropping volumes — this wipes every account, entry and outbox event")
    runner.run([*runner.compose, "down", "-v"], cwd=runner.project_dir, check=False)
    up()
    migrate()


@runner.task("psql", "🐘", "Services", "Open psql on the ledger database")
def psql() -> None:
    runner.run(
        [*runner.compose, "exec", "postgres", "psql", "-U", "ledger", "-d", "ledger"],
        cwd=runner.project_dir,
        check=False,
    )


@runner.task("redis-cli", "🧰", "Services", "Open redis-cli on the idempotency cache")
def redis_cli() -> None:
    runner.run([*runner.compose, "exec", "redis", "redis-cli"], cwd=runner.project_dir, check=False)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


@runner.task("dispatcher", "📮", "Run", "Run with RUN_DISPATCHER=true (webhook outbox, V4)")
def dispatcher() -> None:
    """The dispatcher's first round calls V4's `dispatch_once`, so on the scaffold it
    dies at once and logs the todo that stopped it."""
    runner.step("📮", f"starting {runner.crate} with the webhook dispatcher…")
    runner.uv("run", runner.crate, env={**_env(), "RUN_DISPATCHER": "true"})


@runner.task("dev", "🚀", "Run", "Postgres + redis up, migrate, then run the server")
def dev() -> None:
    deps()
    migrate()
    runner.tasks["run"][0]()


@runner.task("smoke", "🔥", "Run", "Hit /healthz (server must be running)")
def smoke() -> None:
    runner.step("🔥", f"GET {_url('/healthz')}")
    code, body = _request(_url("/healthz"))
    if code == 200:
        runner.ok(f"healthz OK ({body.strip()})")
    else:
        runner.fail(f"healthz failed ({code}) — is the server running?")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


@runner.task("account", "🏦", "Probe", "POST /accounts (NAME= CURRENCY= OVERDRAFT=1) — V1")
def account() -> None:
    body = {
        "name": os.environ.get("NAME", "alice"),
        "currency": os.environ.get("CURRENCY", "USD"),
        "allow_negative": os.environ.get("OVERDRAFT", "").lower() in ("1", "true", "yes"),
    }
    runner.step("🏦", f"POST {_url('/accounts')}  {json.dumps(body)}")
    _report(*_request(_url("/accounts"), body=body))


@runner.task("transfer", "💸", "Probe", "POST /transfers (FROM= TO= AMOUNT= [KEY=]) — V2/V3")
def transfer() -> None:
    body = {
        "from": _require_env("FROM", "the id of the account to debit"),
        "to": _require_env("TO", "the id of the account to credit"),
        # A string from the environment, sent as a JSON *integer*: minor units, rule zero.
        "amount": int(os.environ.get("AMOUNT", "1000")),
        "currency": os.environ.get("CURRENCY", "USD"),
    }
    headers: dict[str, str] = {}
    key = os.environ.get("KEY")
    if key:
        headers["Idempotency-Key"] = key
    runner.step("💸", f"POST {_url('/transfers')}  amount={body['amount']}  key={key or '—'}")
    _report(*_request(_url("/transfers"), body=body, headers=headers))


@runner.task("balance", "⚖️", "Probe", "GET /accounts/$ACCOUNT/balance — V1")
def balance() -> None:
    account_id = _require_env("ACCOUNT", "an id `make account` returned")
    path = f"/accounts/{account_id}/balance"
    runner.step("⚖️", f"GET {_url(path)}")
    _report(*_request(_url(path)))


@runner.task("txn", "🧾", "Probe", "GET /transactions/$TXN — V1")
def txn() -> None:
    txn_id = _require_env("TXN", "a transaction id `make transfer` returned")
    path = f"/transactions/{txn_id}"
    runner.step("🧾", f"GET {_url(path)}")
    _report(*_request(_url(path)))


@runner.task("audit", "🔍", "Probe", "Σ of every entry, and unbalanced transactions — must be 0")
def audit() -> None:
    """Reads the invariants straight out of Postgres, past every line of your code.

    Both numbers hold whatever sign convention V1 picks: a ledger's entries sum to
    zero, and so does each transaction's. This is the "proven from the ledger, not
    vibes" half of the boss fight — run it before and after a storm.
    """
    runner.step("🔍", "auditing the ledger in Postgres")
    _psql(
        "SELECT count(*) AS entries,"
        " COALESCE(sum(amount), 0) AS ledger_sum,"
        " (SELECT count(*) FROM (SELECT 1 FROM entries GROUP BY transaction_id"
        "   HAVING sum(amount) <> 0) unbalanced) AS unbalanced_transactions"
        " FROM entries"
    )


@runner.task("metrics", "📈", "Probe", "GET /metrics — the ledger_* series")
def metrics() -> None:
    code, body = _request(_url("/metrics"))
    if code == 0:
        runner.fail("no answer — is the server running?")
        sys.exit(1)
    lines = [ln for ln in body.splitlines() if ln.startswith("ledger_")]
    if not lines:
        runner.warn("no ledger_* series — is this the right server?")
        return
    for line in lines:
        print(f"  {line}")
    print()
    runner.ok(f"{len(lines)} ledger metric series")


@runner.task("receiver", "📥", "Probe", "A webhook sink on :9000 (RECEIVER_PORT= FAIL=1) — V4")
def receiver() -> None:
    """Prints every delivery's signature headers and body.

    It doesn't verify anything: verifying is V4's `verify`, and a receiver that trusted
    its own copy would prove nothing about yours. With FAIL=1 it answers 500 to
    everything, which is how you watch backoff spread the retries out and an event
    reach the DLQ — `make psql`, then look at `webhook_outbox`.
    """
    # Not `PORT`: that one is the ledger server's, and both run side by side.
    port = int(os.environ.get("RECEIVER_PORT", "9000"))
    fail = os.environ.get("FAIL", "").lower() in ("1", "true", "yes")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length).decode("utf-8", "replace")
            status = 500 if fail else 200
            color = C.RED if fail else C.GREEN
            print(f"  {color}{status}{C.RESET}  POST {self.path}")
            print(f"   {C.DIM}X-Timestamp: {self.headers.get('x-timestamp')}{C.RESET}")
            print(f"   {C.DIM}X-Signature: {self.headers.get('x-signature')}{C.RESET}")
            print(f"   {body}")
            self.send_response(status)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    mode = "answering 500 to everything" if fail else "answering 200"
    runner.step("📥", f"webhook receiver on http://localhost:{port}/ — {mode} (Ctrl-C to stop)")
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()
            runner.ok("receiver stopped")


# --------------------------------------------------------------------------- #
# The boss fight
# --------------------------------------------------------------------------- #


@runner.task("bench", "🐉", "Bench", "The Double Spend: a transfer storm on one hot account")
def bench() -> None:
    """The boss fight's harness — building it is part of the fight.

    Seed accounts with a known total, run a mixed multi-account workload and a
    single-hot-account storm against the production container, replay a fraction of
    requests with their original `Idempotency-Key`, and `make audit` before and after.
    """
    runner.warn("bench/ is yours to build — see the 🐉 Boss fight in SPEC.md")
    print(f"   {C.DIM}make audit   # the conservation sum, before and after{C.RESET}")
    print(f"   {C.DIM}make md      # read the Arena + 'the boss falls when'{C.RESET}")


@runner.task("profile", "🔥", "Bench", "Sample the running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process by PID, so start the server, drive a
    transfer storm at it, and sample while it's under load — a flamegraph of an idle
    loop tells you nothing. The question it answers: of each transfer's time, how much
    is Postgres, and how much is Python?
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    pid = os.environ.get("PID")
    if not pid:
        runner.fail("set PID=<server pid> — py-spy samples a running process")
        print(f"   {C.DIM}e.g. `PID=$(pgrep -f ledger-payments-core) make profile`{C.RESET}")
        sys.exit(1)
    runner.step("🔥", "sampling for 10s — keep the transfer storm running meanwhile")
    runner.uv("run", "py-spy", "record", "--duration", "10", "--pid", pid, "--output", str(out))
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
