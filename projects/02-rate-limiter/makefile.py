#!/usr/bin/env python3
"""rate-limiter — local dev task runner.

A small wrapper around the day-to-day commands for this project (docker, uv,
protobuf codegen, the gRPC smoke probe). The `Makefile` shells out to this file
so you get one source of truth with colors, emojis and readable output. Help
tables use `tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import sys
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
    register_redis,
    register_setup,
)

# gRPC service coordinates for the `smoke` probe (see proto/ratelimit.proto).
PROTO = "ratelimit.proto"
GRPC_SERVICE = "ratelimit.v1.RateLimiter"
# Where `make proto` writes generated bindings — committed, see pb/__init__.py.
PB_DIR = "src/rate_limiter/pb"

runner = make_runner(
    crate="rate-limiter",
    help_title="🚦 rate-limiter",
    project_dir=PROJECT_DIR,
    help_footers=[
        ("Typical first run", "make setup && make sync && make dev"),
        ("Introspect it", "grpcurl -plaintext localhost:50051 list"),
        ("Before you commit", "make verify"),
    ],
    default_port="50051",
)

register_setup(runner)
register_python_checks(runner)
register_compose_lifecycle(runner)
redis = register_redis(runner, default_port=6302, include_reset=True)
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
    # protoc emits a top-level `import ratelimit_pb2`, which only resolves if the
    # generated files sit on sys.path. They live in a package, so rewrite it to a
    # relative import. Every Python gRPC project hits this; there is no protoc
    # flag for it.
    for name in ("ratelimit_pb2_grpc.py", "ratelimit_pb2_grpc.pyi"):
        path = runner.project_dir / PB_DIR / name
        text = path.read_text()
        path.write_text(
            text.replace("\nimport ratelimit_pb2 as ", "\nfrom . import ratelimit_pb2 as ")
        )
    runner.ok("bindings regenerated — commit them")


@runner.task("up", "🐳", "Services", "Start Redis (docker compose up -d)")
def up() -> None:
    runner.step("🐳", "starting Redis…")
    runner.run([*runner.compose, "up", "-d"], cwd=runner.project_dir)
    redis["wait_redis"]()


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("dev", "🚀", "Run", "sync, start Redis, then run the server")
def dev() -> None:
    sync()
    deps()
    runner.tasks["run"][0]()


@runner.task("smoke", "🔥", "Run", "gRPC Peek probe (server must be running; needs grpcurl)")
def smoke() -> None:
    """Probes over the wire, through server reflection.

    No `-proto` flag: `main` registers the reflection service, so grpcurl asks
    the server what it serves. That the probe works *without* a local copy of
    the .proto is the checklist item being demonstrated.
    """
    runner.require("grpcurl", "Install grpcurl: https://github.com/fullstorydev/grpcurl")
    port = runner.load_dotenv().get("PORT", "50051")
    runner.step("🔥", f"gRPC {GRPC_SERVICE}/Peek on localhost:{port}")
    rc = runner.run(
        [
            "grpcurl",
            "-plaintext",
            "-d",
            '{"key": "smoke"}',
            f"localhost:{port}",
            f"{GRPC_SERVICE}/Peek",
        ],
        cwd=runner.project_dir,
        check=False,
    )
    if rc == 0:
        runner.ok("Peek OK")
    else:
        runner.fail("Peek failed — is the server running? (a V3 todo also fails here)")
        sys.exit(1)


@runner.task("health", "🩺", "Run", "Hit the admin /healthz (server must be running)")
def health() -> None:
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("METRICS_PORT", "9102")
    runner.step("🩺", f"GET http://localhost:{port}/healthz")
    rc = runner.run(["curl", "-sf", f"http://localhost:{port}/healthz"], check=False)
    print()
    if rc == 0:
        runner.ok("healthz OK")
    else:
        runner.fail("healthz failed — is the server running?")
        sys.exit(1)


@runner.task("profile", "🔥", "Bench", "Sample the running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so drive load at it while this runs —
    a flamegraph of an idle server tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some load at the server meanwhile")
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
        "import rate_limiter.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
