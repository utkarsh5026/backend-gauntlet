#!/usr/bin/env python3
"""api-gateway — local dev task runner.

A small wrapper around the day-to-day commands for this crate (docker, cargo,
and the probes that make the gateway's behaviour *visible*: which backend served
a request, what headers the backend actually received). The `Makefile` shells out
to this file so you get one source of truth with colors, emojis and readable
output. Help tables use `tools/makefile_help.py` (Rich — auto-installed from
`tools/requirements.txt`).

The dev loop this is built around: the **backend pool runs in Docker**
(`make up` → three `traefik/whoami` containers on :9010-:9012) while the
**gateway runs locally** (`make run` → `cargo run`), so you get compile-and-go
iteration instead of a container rebuild per change. `make demo` runs the fully
containerized stack instead.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    C,
    make_runner,
    register_cargo_checks,
    register_compose_lifecycle,
    register_help,
    register_md,
    register_run,
    register_setup,
    register_smoke_healthz,
)

# The echo-backend pool from docker-compose.yml. Each container reports its own
# name in the response body, so a burst of requests *shows* the load balancer's
# spread (V3) and a stopped container *shows* fail-fast + reroute (V4).
BACKENDS = ("whoami-a", "whoami-b", "whoami-c")
BACKEND_PORTS = (9010, 9011, 9012)

runner = make_runner(
    crate="api-gateway",
    help_title="🚪 api-gateway",
    project_dir=PROJECT_DIR,
    help_footers=[
        ("Typical first run", "make setup && make up && make run"),
        (
            "See the balancer work",
            "make spread   (then: make kill-backend && make spread)",
        ),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_cargo_checks(runner)
compose = register_compose_lifecycle(runner)
run_server = register_run(runner)
register_smoke_healthz(runner)


def _gateway_url(path: str = "") -> str:
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    return f"http://localhost:{port}{path}"


def _get(url: str, headers: dict[str, str] | None = None, timeout: float = 5.0):
    """GET `url`, returning (status, body). Raises on transport failure."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _require_gateway() -> None:
    """Fail with a useful hint when the gateway isn't up yet."""
    try:
        _get(_gateway_url("/healthz"), timeout=2.0)
    except OSError:
        runner.fail(f"no gateway on {_gateway_url()} — start it with `make run`")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Services — the backend pool
# --------------------------------------------------------------------------- #


@runner.task(
    "wait-backends", "⏳", "Services", "Block until every whoami backend answers"
)
def wait_backends() -> None:
    import time

    runner.step("⏳", f"waiting for {len(BACKEND_PORTS)} backends…")
    for port in BACKEND_PORTS:
        for _ in range(30):
            if runner.port_open("127.0.0.1", port):
                break
            time.sleep(1)
        else:
            runner.fail(f"backend on :{port} never came up")
            sys.exit(1)
    runner.ok(f"backends ready on {', '.join(f':{p}' for p in BACKEND_PORTS)}")


@runner.task(
    "up", "🐳", "Services", "Start the whoami backend pool (gateway runs locally)"
)
def up() -> None:
    runner.step("🐳", "starting the echo-backend pool…")
    runner.run([*runner.compose, "up", "-d", *BACKENDS], cwd=runner.project_dir)
    wait_backends()
    print(
        f"   {C.DIM}gateway itself is not started — use `make run` (or `make demo`){C.RESET}"
    )


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task(
    "kill-backend", "💀", "Services", "Stop one backend to exercise V4 (B=whoami-b)"
)
def kill_backend() -> None:
    target = os.environ.get("B", "whoami-b")
    runner.warn(f"stopping {target} — the gateway should reroute around it")
    runner.run([*runner.compose, "stop", target], cwd=runner.project_dir)
    print(
        f"   {C.DIM}now: `make spread` (V3 reroute) · `make revive-backend` to undo{C.RESET}"
    )


@runner.task(
    "revive-backend", "❤️‍🩹", "Services", "Restart a stopped backend (B=whoami-b)"
)
def revive_backend() -> None:
    target = os.environ.get("B", "whoami-b")
    runner.step("❤️‍🩹", f"starting {target}…")
    runner.run([*runner.compose, "start", target], cwd=runner.project_dir)
    wait_backends()


@runner.task("dev", "🚀", "Run", "Start the backend pool, then run the gateway")
def dev() -> None:
    up()
    run_server()


@runner.task(
    "demo", "🎬", "Run", "Fully containerized stack (gateway + pool, foreground)"
)
def demo() -> None:
    runner.rule(C.MAGENTA)
    print(
        f"{C.BOLD}{C.MAGENTA}🎬  Building and running the whole stack in Docker{C.RESET}"
    )
    print(
        f"   {C.DIM}gateway on {C.RESET}{C.BOLD}{C.CYAN}http://localhost:8080{C.RESET} "
        f"{C.DIM}· in another shell: make spread / make kill-backend{C.RESET}"
    )
    runner.rule(C.MAGENTA)
    runner.run([*runner.compose, "up", "--build"], cwd=runner.project_dir, check=False)


@runner.task("routes", "🗺️", "Probe", "GET /admin/routes — the loaded route table")
def routes() -> None:
    _require_gateway()
    status, body = _get(_gateway_url("/admin/routes"))
    print(body.strip())
    if status != 200:
        runner.fail(f"unexpected status {status}")
        sys.exit(1)


@runner.task(
    "spread", "🎲", "Probe", "Send N requests and tally which backend served (N=30)"
)
def spread() -> None:
    _require_gateway()
    n = int(os.environ.get("N", "30"))
    url = _gateway_url("/")
    runner.step("🎲", f"{n} requests → {url}")

    # Backends that actually served, kept apart from failures — a pool that
    # "spread evenly" across six identical errors is not a working balancer.
    served: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    for _ in range(n):
        try:
            status, body = _get(url)
        except OSError as exc:
            failed[f"transport error: {exc}"] += 1
            continue
        if status != 200:
            failed[f"HTTP {status}"] += 1
            continue
        # traefik/whoami echoes `Name: <container>` as its first line.
        name = next(
            (
                ln.split(":", 1)[1].strip()
                for ln in body.splitlines()
                if ln.startswith("Name:")
            ),
            "200 (unrecognized body)",
        )
        served[name] += 1

    print()
    width = max((len(k) for k in [*served, *failed]), default=10)
    for name, count in served.most_common():
        bar = "█" * round(40 * count / n)
        print(
            f"  {C.GREEN}{name:<{width}}{C.RESET}  {count:>4}  {C.CYAN}{bar}{C.RESET}"
        )
    for name, count in failed.most_common():
        print(f"  {C.RED}{name:<{width}}{C.RESET}  {count:>4}")
    print()

    if not served:
        runner.fail(f"0/{n} requests were served — the proxy path still has todo!()s")
        print(
            f"   {C.DIM}check the server log: the panic names the vertical it needs{C.RESET}"
        )
        sys.exit(1)
    if failed:
        runner.warn(f"{sum(failed.values())}/{n} requests failed")
    if len(served) == 1:
        runner.warn(
            "every served request hit one backend — expect a spread once V3's pick() is real"
        )
    else:
        runner.ok(f"{len(served)} distinct backends served across {n} requests")


@runner.task(
    "headers", "🧾", "Probe", "Send hostile headers and print what the backend saw (V1)"
)
def headers() -> None:
    _require_gateway()
    hostile = {
        "X-Forwarded-For": "1.2.3.4",
        "X-Forwarded-Proto": "https",
        "Connection": "keep-alive, X-Secret-Hop",
        "X-Secret-Hop": "should-not-arrive",
        "Proxy-Authorization": "Basic c2hvdWxkLW5vdC1hcnJpdmU=",
        "TE": "trailers",
    }
    runner.step(
        "🧾", "sending hop-by-hop + spoofed provenance headers through the gateway"
    )
    for k, v in hostile.items():
        print(f"   {C.DIM}sent  {k}: {v}{C.RESET}")
    print()
    try:
        status, body = _get(_gateway_url("/"), headers=hostile)
    except OSError as exc:
        runner.fail(f"the proxied request never completed ({exc})")
        print(
            f"   {C.DIM}the proxy path still has todo!()s — the server log names which{C.RESET}"
        )
        sys.exit(1)
    print(f"{C.BOLD}backend saw (HTTP {status}):{C.RESET}")
    print(body.strip())
    print()
    print(
        f"{C.DIM}V1 wants: no Connection/TE/Proxy-* /X-Secret-Hop above, and an "
        f"X-Forwarded-For your gateway appended to (not 1.2.3.4 alone).{C.RESET}"
    )


@runner.task("metrics", "📈", "Probe", "GET /metrics — the Prometheus scrape")
def metrics() -> None:
    _require_gateway()
    _, body = _get(_gateway_url("/metrics"))
    lines = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
    if not lines:
        runner.warn(
            "registry is empty — no counters recorded yet (observability horizontal)"
        )
        return
    for line in lines:
        print(f"  {line}")
    print()
    runner.ok(f"{len(lines)} metric series")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
