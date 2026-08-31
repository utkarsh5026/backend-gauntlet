#!/usr/bin/env python3
"""workflow-engine — local dev task runner.

A small wrapper around the day-to-day commands for this project (docker, uv,
migrations, protobuf codegen, the gRPC smoke probe). The `Makefile` shells out to
this file so you get one source of truth with colors, emojis and readable output.
Help tables use `tools/makefile_help.py` (Rich — auto-installed from
`tools/requirements.txt`).

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    make_runner,
    register_compose_lifecycle,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
)

# gRPC service coordinates for the `smoke` probe (see proto/workflow.proto).
PROTO = "workflow.proto"
GRPC_SERVICE = "workflow.v1.WorkflowService"
# Where `make proto` writes generated bindings — committed, see pb/__init__.py.
PB_DIR = "src/workflow_engine/pb"

runner = make_runner(
    crate="workflow-engine",
    help_title="⏳ workflow-engine",
    project_dir=PROJECT_DIR,
    default_port="7233",
    help_footers=[
        ("Typical first run", "make setup && make sync && make dev"),
        ("Introspect it", "grpcurl -plaintext localhost:7233 list"),
        ("Timers are off by default", "RUN_TIMER_SERVICE=true make run"),
        ("Before you commit", "make verify"),
        ("Prod-parity: run the app in Docker too", "make dev-container"),
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


@runner.task("proto", "🧬", "Setup", "Regenerate the gRPC bindings from proto/")
def proto() -> None:
    """The Python stand-in for the old `build.rs`.

    `grpcio-tools` carries its own `protoc`, so this needs no system protobuf —
    the same guarantee `protoc-bin-vendored` gave the Rust build. `mypy-protobuf`
    emits the `.pyi` beside each module, which is what keeps callers type-safe
    under pyright strict even though grpcio ships no type information itself.
    """
    runner.step("🧬", f"generating {PB_DIR} from proto/{PROTO}…")
    runner.uv(
        "run",
        "python",
        "-m",
        "grpc_tools.protoc",
        "-Iproto",
        f"--python_out={PB_DIR}",
        f"--grpc_python_out={PB_DIR}",
        f"--mypy_out=quiet:{PB_DIR}",
        f"--mypy_grpc_out=quiet:{PB_DIR}",
        f"proto/{PROTO}",
    )
    # protoc emits a top-level `import workflow_pb2`, which only resolves if the
    # generated files sit on sys.path. They live in a package, so rewrite it to a
    # relative import. Every Python gRPC project hits this; there is no protoc
    # flag for it.
    for name in ("workflow_pb2_grpc.py", "workflow_pb2_grpc.pyi"):
        path = runner.project_dir / PB_DIR / name
        text = path.read_text()
        path.write_text(
            text.replace("\nimport workflow_pb2 as ", "\nfrom . import workflow_pb2 as ")
        )
    runner.ok("bindings regenerated — commit them")


@runner.task("wait-db", "⏳", "Services", "Block until Postgres accepts connections")
def wait_db() -> None:
    runner.step("⏳", "waiting for Postgres…")
    for _ in range(30):
        probe = subprocess.run(
            [*runner.compose, "exec", "-T", "postgres", "pg_isready", "-U", "workflow"],
            cwd=str(runner.project_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            runner.ok("Postgres is ready")
            return
        time.sleep(1)
    runner.fail("Postgres did not become ready in time")
    sys.exit(1)


@runner.task("migrate", "🗃️", "Services", "Apply SQL migrations")
def migrate() -> None:
    """The Python answer to `sqlx migrate run`.

    The runner lives in `src/workflow_engine/migrate.py`, so migrating needs
    nothing installed beyond this project's own dependencies — no sqlx-cli, no
    second toolchain to keep in step with the code.
    """
    runner.step("🗃️", "applying migrations…")
    runner.uv(
        "run",
        "python",
        "-m",
        "workflow_engine.migrate",
        str(runner.project_dir / "migrations"),
        env=runner.load_dotenv(),
    )
    runner.ok("migrations applied")


@runner.task("up", "🐳", "Services", "Start Postgres (history, task queues, timers)")
def up() -> None:
    runner.step("🐳", "starting Postgres…")
    runner.run([*runner.compose, "up", "-d", "postgres"], cwd=runner.project_dir)
    wait_db()


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("reset-db", "💥", "Services", "Drop volumes and recreate the DB (destructive)")
def reset_db() -> None:
    runner.warn("dropping volumes — this wipes every history, task and timer")
    runner.run([*runner.compose, "down", "-v"], cwd=runner.project_dir, check=False)
    up()
    migrate()


@runner.task("db-ui", "🔭", "Services", "Open pgweb — browse the history log at :8021")
def db_ui() -> None:
    runner.step("🔭", "starting pgweb (Postgres browser UI)…")
    runner.run([*runner.compose, "up", "-d", "pgweb"], cwd=runner.project_dir)
    runner.ok("pgweb is up → http://localhost:8021 (watch history_events grow)")


@runner.task("dev", "🚀", "Run", "Start deps, migrate, then run the engine")
def dev() -> None:
    deps()
    migrate()
    runner.tasks["run"][0]()


@runner.task(
    "dev-container",
    "🐋",
    "Run",
    "Prod-parity loop: deps, migrate, then run the engine itself in Docker",
)
def dev_container() -> None:
    deps()
    migrate()
    runner.step("🐋", "building + starting workflow-engine in Docker…")
    runner.run(
        [*runner.compose, "up", "-d", "--build", "workflow-engine"],
        cwd=runner.project_dir,
    )
    runner.ok("engine is up → grpc :7233, admin :9121 (make logs to follow it)")


@runner.task("smoke", "🔥", "Run", "gRPC reflection probe (server must be running; needs grpcurl)")
def smoke() -> None:
    """Probes over the wire, through server reflection.

    No `-proto` flag: `main` registers the reflection service, so grpcurl asks
    the server what it serves. That the probe works *without* a local copy of the
    .proto is the checklist item being demonstrated.
    """
    runner.require("grpcurl", "Install grpcurl: https://github.com/fullstorydev/grpcurl")
    port = runner.load_dotenv().get("PORT", "7233")
    runner.step("🔥", f"listing {GRPC_SERVICE} methods on localhost:{port}")
    rc = runner.run(
        ["grpcurl", "-plaintext", f"localhost:{port}", "list", GRPC_SERVICE],
        cwd=runner.project_dir,
        check=False,
    )
    if rc == 0:
        runner.ok("reflection OK — the contract is discoverable over the wire")
    else:
        runner.fail("reflection failed — is the engine running?")
        sys.exit(1)


@runner.task("health", "🩺", "Run", "Hit the admin /healthz (server must be running)")
def health() -> None:
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("METRICS_PORT", "9121")
    runner.step("🩺", f"GET http://localhost:{port}/healthz")
    rc = runner.run(["curl", "-sf", f"http://localhost:{port}/healthz"], check=False)
    print()
    if rc == 0:
        runner.ok("healthz OK")
    else:
        runner.fail("healthz failed — is the engine running?")
        sys.exit(1)


@runner.task("profile", "🔥", "Bench", "Sample a running engine with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so drive load at it while this runs —
    a flamegraph of an idle event loop tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some workflow load meanwhile")
    runner.uv(
        "run",
        "py-spy",
        "record",
        "--duration",
        "10",
        "--output",
        str(out),
        "--",
        "python",
        "-c",
        "import workflow_engine.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
