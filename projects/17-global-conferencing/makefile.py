#!/usr/bin/env python3
"""global-conferencing — local dev task runner.

A wrapper around the day-to-day commands for this project (uv, one SFU or a
whole mesh, and the probes that make a *cascade* visible). The `Makefile` shells
out to this file so there is one source of truth with colors, emojis and
readable output. Help tables use `tools/makefile_help.py`.

There is deliberately **no docker-compose**: the placement map is consensus this
process builds (V1), the relay mesh is SFU-to-SFU UDP (V2), recordings go to
local disk (V4). So the runner has no compose/db bundles — it has a mesh.

The probes separate failures that look identical from a browser ("I joined and
see nobody"):

* `make planes`  — is each plane up? HTTP is TCP and easy; media and the
                   backbone are UDP and need an honest probe.
* `make publish` — signaling made visible; the first publish *places* the room.
* `make rooms`   — the topology this node agrees on. Run it against every region
                   (`HTTP_PORT=8081 make rooms`): three nodes, one answer, or a
                   split room.
* `make vote`    — send a RequestVote the way a peer would (V1's first RPC).
* `make relay`   — send a datagram at the backbone, the way a peer would (V2).
* `make mesh`    — three regions on one host, wired to each other.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    C,
    launch_mprocs,
    make_runner,
    register_dev_stack,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
)

runner = make_runner(
    crate="global-conferencing",
    help_title="🌍 global-conferencing (placement · relay mesh · routing · recording)",
    project_dir=PROJECT_DIR,
    # Signaling + cluster control + admin. MEDIA_PORT and CASCADE_PORT are UDP.
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("See all three planes", "make planes"),
        ("Place a room (V1)", "make publish  (then: make rooms)"),
        ("Three regions on one host", "make mesh   (then: HTTP_PORT=8081 make rooms)"),
        ("Poke the backbone (V2)", "make relay"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
run_server = register_python_run(runner)
# web/ exists → full-stack `dev` (server + Vite) plus `web-install` / `frontend`.
register_dev_stack(runner)

MESH = (
    # region, node id, http, media, cascade
    ("eu-west", "n1", 8080, 7000, 7100),
    ("us-east", "n2", 8081, 7001, 7101),
    ("ap-south", "n3", 8082, 7002, 7102),
)
"""The three-region mesh `make mesh` launches — the SPEC's "Run it" layout."""


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


def _setting(key: str, default: str) -> str:
    """The process environment beats `.env` — same precedence as `Settings`."""
    return os.environ.get(key) or runner.load_dotenv().get(key, default)


def _http_port() -> int:
    return int(_setting("HTTP_PORT", runner.config.default_port))


def _url(path: str = "") -> str:
    return f"http://localhost:{_http_port()}{path}"


def _request(
    path: str, payload: dict[str, object] | None = None, timeout: float = 5.0
) -> tuple[int, str]:
    request = urllib.request.Request(
        _url(path),
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def _require_server() -> None:
    if not runner.port_open("127.0.0.1", _http_port()):
        runner.fail(f"no SFU on :{_http_port()} — start one with `make run` or `make mesh`")
        sys.exit(1)


def _show_reply(status: int, body: str) -> None:
    """Print a reply, calling out the scaffold's 501 as the worklist it is."""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    if status == 501 and isinstance(parsed, dict) and "todo" in parsed:
        runner.warn(f"501 — not built yet: {parsed['todo']}")
        print(f"   {C.DIM}that is the worklist, not a bug{C.RESET}")
    elif 200 <= status < 300:
        print(json.dumps(parsed, indent=2) if parsed is not None else body)
    else:
        runner.fail(f"status {status}: {body.strip()}")


def _udp_reachable(host: str, port: int, timeout: float = 0.5) -> bool | None:
    """`True` = something answered, `False` = refused (ICMP), `None` = no idea.

    UDP has no connect. The only negative signal is an ICMP port-unreachable,
    which a `connect`ed socket surfaces as `ECONNREFUSED` on the next call.
    Silence means open, or firewalled, or still in flight — so `None` is honest.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.send(b"\x00")
            try:
                sock.recv(2048)
            except TimeoutError:
                return None
            except ConnectionRefusedError:
                return False
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


@runner.task("smoke", "🔥", "Run", "Hit /healthz on HTTP_PORT (server must be running)")
def smoke() -> None:
    runner.step("🔥", f"GET {_url('/healthz')}")
    status, body = _request("/healthz")
    if status == 200:
        runner.ok(f"healthz {body.strip()}")
    else:
        runner.fail("healthz failed — is the server running?")
        sys.exit(1)


@runner.task("mesh", "🌍", "Run", "Three regional SFUs on one host (mprocs, one pane each)")
def mesh() -> None:
    """The SPEC's 3-region layout, each node's PEERS naming the other two.

    Environment variables beat `.env`, so each pane overrides identity, ports and
    PEERS while sharing everything else. `RUN_BACKGROUND` passes through: leave it
    off until V1 exists, then `RUN_BACKGROUND=true make mesh` runs real elections.
    """
    background = os.environ.get("RUN_BACKGROUND", "false")
    procs: dict[str, dict[str, str]] = {}
    for region, node_id, http, media, cascade in MESH:
        peers = ",".join(
            f"{other}=http://127.0.0.1:{o_http}|127.0.0.1:{o_cascade}"
            for other, _, o_http, _, o_cascade in MESH
            if other != region
        )
        env = (
            f"REGION={region} NODE_ID={node_id} HTTP_PORT={http} MEDIA_PORT={media} "
            f"CASCADE_PORT={cascade} PEERS='{peers}' RUN_BACKGROUND={background}"
        )
        procs[region] = {
            "shell": f"exec env {env} uv run global-conferencing",
            "cwd": str(PROJECT_DIR),
        }
        print(
            f"   {C.CYAN}{region:<9}{C.RESET} http :{http}  media :{media}/udp  "
            f"backbone :{cascade}/udp"
        )
    print(f"   {C.DIM}then: HTTP_PORT=8081 make rooms — every region should agree{C.RESET}")
    launch_mprocs(procs)


# --------------------------------------------------------------------------- #
# Probes — the criteria, made visible
# --------------------------------------------------------------------------- #


@runner.task("planes", "🛰️", "Probe", "Who is up: HTTP, the media UDP port, the backbone UDP port")
def planes() -> None:
    http_up = runner.port_open("127.0.0.1", _http_port())
    rows = [
        ("signaling + cluster", _http_port(), "TCP", True if http_up else False),
        ("media (muxed)", int(_setting("MEDIA_PORT", "7000")), "UDP", None),
        ("backbone (cascade)", int(_setting("CASCADE_PORT", "7100")), "UDP", None),
    ]
    marks = {
        True: f"{C.GREEN}●{C.RESET}",
        False: f"{C.RED}●{C.RESET}",
        None: f"{C.YELLOW}◐{C.RESET}",
    }
    print()
    for name, port, proto, known in rows:
        state = known if proto == "TCP" else _udp_reachable("127.0.0.1", port)
        label = {True: "up", False: "refused", None: "no ICMP"}[state]
        print(f"  {marks[state]} {name:<20} :{port:<6} {label:<10} {C.DIM}{proto}{C.RESET}")
    print()
    if http_up:
        status, _ = _request("/readyz")
        if status == 200:
            runner.ok("ready — the backbone pump is draining")
        elif status == 503:
            runner.warn("HTTP is up but the backbone pump is dead (V2 raised?)")
    print(f"   {C.DIM}'no ICMP' is normal for UDP — see `_udp_reachable`{C.RESET}\n")


@runner.task("publish", "📡", "Probe", "POST a 3-layer publish (ROOM=all-hands) — places the room")
def publish() -> None:
    """The first publish for a new room is a consensus decision (V1).

    Run it against two regions at once (`make publish & HTTP_PORT=8081 make
    publish`) once V1 works: both must answer with the *same* `home_region`.
    """
    _require_server()
    room = os.environ.get("ROOM", "all-hands")
    payload: dict[str, object] = {
        "layers": [
            {"rid": "q", "ssrc": 111, "bitrate_bps": 150_000},
            {"rid": "h", "ssrc": 222, "bitrate_bps": 500_000},
            {"rid": "f", "ssrc": 333, "bitrate_bps": 2_000_000},
        ],
        "client_ufrag": "probepub",
    }
    runner.step("📡", f"POST {_url(f'/rooms/{room}/publish')}")
    _show_reply(*_request(f"/rooms/{room}/publish", payload))
    print()


@runner.task("sub", "👀", "Probe", "Subscribe to a publisher (ROOM=, PUBLISHER=1)")
def sub() -> None:
    """Subscribing from a region that is not the room's home ensures a relay leg (V2)."""
    _require_server()
    room = os.environ.get("ROOM", "all-hands")
    publisher = int(os.environ.get("PUBLISHER", "1"))
    runner.step("👀", f"POST {_url(f'/rooms/{room}/subscribe')}  publisher={publisher}")
    _show_reply(
        *_request(
            f"/rooms/{room}/subscribe",
            {"publisher": publisher, "client_ufrag": "probesub"},
        )
    )
    print()


@runner.task("rooms", "🗺️", "Probe", "GET /rooms — the global topology this node agrees on")
def rooms() -> None:
    _require_server()
    status, body = _request("/rooms")
    if status != 200:
        runner.fail(f"unexpected status {status}: {body.strip()}")
        sys.exit(1)
    topology = json.loads(body)
    print(f"\n  {C.BOLD}as seen from {topology['region']}{C.RESET}")
    if not topology["rooms"]:
        print(f"  {C.DIM}no rooms placed — `make publish`{C.RESET}")
    for room in topology["rooms"]:
        print(
            f"  {C.CYAN}{room['room_id']:<16}{C.RESET} home={room['home_region']:<10} "
            f"active={','.join(room['active_regions']) or '-'}  epoch={room['epoch']}"
        )
    for leg in topology["relay_legs"]:
        print(
            f"  {C.GREEN}leg{C.RESET} → {leg['region']:<10} {leg['remote_addr']}  "
            f"tracks={leg['tracks']}"
        )
    print()


@runner.task("status", "📊", "Probe", "GET /status — role, term, legs, recordings, planes")
def status() -> None:
    _require_server()
    _show_reply(*_request("/status"))


@runner.task("metrics", "📈", "Probe", "GET /metrics — the conf_* series")
def metrics() -> None:
    _, body = _request("/metrics")
    lines = [ln for ln in body.splitlines() if ln.startswith("conf_")]
    if not lines:
        runner.warn("no conf_* series — is the server running?")
        return
    for line in lines:
        print(f"  {line}")
    print()
    runner.ok(f"{len(lines)} series")


@runner.task("vote", "🗳️", "Probe", "POST /cluster/vote as a peer would (V1)")
def vote() -> None:
    """A RequestVote from a made-up candidate at term 1.

    On the bare scaffold: 501 naming `on_vote`. Once V1 works, send it twice —
    the second grant at the same term must be `false` (one vote per term).
    """
    _require_server()
    runner.step("🗳️", f"POST {_url('/cluster/vote')}  candidate=probe term=1")
    _show_reply(*_request("/cluster/vote", {"candidate_id": "probe", "term": 1}))
    print()


@runner.task("relay", "📦", "Probe", "Send a datagram at the backbone port as a peer would (V2)")
def relay() -> None:
    """A stranger's datagram on the backbone.

    On the bare scaffold it reaches `CascadeMesh.on_relayed`, which raises: the
    pump dies and `/readyz` turns 503. Once V2 works, this — from an address not
    in PEERS — must be *dropped*, `conf_relay_dropped_total{reason="unknown_peer"}`
    must tick, and `/readyz` must stay green.
    """
    port = int(_setting("CASCADE_PORT", "7100"))
    runner.step("📦", f"sending 16 bytes to 127.0.0.1:{port}/udp")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b"not-a-real-peer!", ("127.0.0.1", port))
    if runner.port_open("127.0.0.1", _http_port()):
        code, _ = _request("/readyz")
        if code == 200:
            runner.ok("/readyz still 200 — the backbone shrugged it off")
        else:
            runner.warn(f"/readyz {code} — the pump died on it (check the log for the V2 todo)")


# --------------------------------------------------------------------------- #
# Bench
# --------------------------------------------------------------------------- #


@runner.task("bench", "🐉", "Bench", "The Hairpin: 3 regions, 50+ subscribers each, a backbone sag")
def bench() -> None:
    """The boss fight's harness — not implemented for you; building it is the fight.

    Decide before writing a line of it: the harness must not share a GIL with the
    SFUs it measures (separate processes or containers); "0 extra backbone
    packets when subscribers double" is read from `conf_relay_copies_out_total`
    rates, not from the harness; and the partition timeline needs
    `conf_node_role`/`conf_node_term` sampled from *every* node, because "a single
    leader" is a claim about all of them at once.
    """
    runner.warn("bench/ is yours to build — see the 🐉 Boss fight section of SPEC.md")
    print(f"   {C.DIM}make md   # the Arena + 'the boss falls when' lines{C.RESET}")
    print(f"   {C.DIM}sudo tc qdisc add dev lo root netem delay 120ms 20ms{C.RESET}")


@runner.task("profile", "🔥", "Bench", "Sample a running SFU with py-spy (PID=…, 10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate. Sample under load, not idle."""
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    pid = os.environ.get("PID")
    if not pid:
        runner.fail("set PID=<sfu pid> — py-spy samples a running process")
        print(
            f"   {C.DIM}e.g. `PID=$(pgrep -f global-conferencing | head -1) make profile`{C.RESET}"
        )
        sys.exit(1)
    runner.step("🔥", "sampling for 10s — drive the mesh meanwhile")
    runner.uv("run", "py-spy", "record", "--duration", "10", "--pid", pid, "--output", str(out))
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
