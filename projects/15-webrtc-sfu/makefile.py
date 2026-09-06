#!/usr/bin/env python3
"""webrtc-sfu — local dev task runner.

A wrapper around the day-to-day commands for this project (uv, the server, and
the probes that make a *media* server visible — which matters more here than in
most projects, because almost nothing interesting happens over HTTP). The
`Makefile` shells out to this file so there is one source of truth with colors,
emojis and readable output. Help tables use `tools/makefile_help.py` (Rich —
auto-installed from `tools/requirements.txt`).

There is deliberately **no docker-compose** here: the SFU *is* the media server,
and the room graph, the ICE agents, the rewriters and the estimators all live
in-process. So this runner has no compose/db bundles — just checks, run, the
browser playground, and the probes.

The probe tasks are the reason this file exists. A media server fails silently
in a way a CRUD service does not: "the video does not play" looks identical
whether signaling never created the peer, ICE never nominated a path, the
selector is dropping every layer, or the rewriter is emitting gaps. These
separate those from each other, in that order:

* `make planes`  — are both planes even up? Signaling is TCP and easy; the media
                   port is UDP and needs an actual probe (see `_udp_reachable`).
* `make publish` — signaling made visible: create a publisher with three
                   simulcast layers, which is exactly V3's input.
* `make sub`     — attach a subscriber and read back the stable SSRC it will
                   receive on for the whole session (V2's outbound identity).
* `make rooms`   — the room graph the fan-out walks.
* `make stun`    — V1 made visible: send a real, hand-built STUN Binding request
                   at the media port and show exactly what came back.
* `make bench`   — the boss fight's load generator (yours to build).

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
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
    make_runner,
    register_dev_stack,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
)

STUN_MAGIC_COOKIE = 0x2112A442
"""Bytes 4..8 of every RFC 5389 message. Spelled out here so `make stun` can
build a real request without importing the package it is probing."""

runner = make_runner(
    crate="webrtc-sfu",
    help_title="🛰️  webrtc-sfu (ICE · RTP fan-out · simulcast · BWE)",
    project_dir=PROJECT_DIR,
    # The signaling + admin port. The media plane is MEDIA_PORT, and it is UDP.
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("See both planes", "make planes"),
        ("Build a room", "make publish  (then: make sub, make rooms)"),
        ("Poke the media port (V1)", "make stun"),
        ("Browser playground", "make dev  (server + Vite)"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
run_server = register_python_run(runner)
# web/ exists → auto full-stack `dev` (server + Vite) plus `web-install` / `frontend`.
register_dev_stack(runner, use_cargo_watch=False)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


def _env() -> dict[str, str]:
    return runner.load_dotenv()


def _http_port() -> int:
    return int(_env().get("HTTP_PORT", runner.config.default_port))


def _media_port() -> int:
    return int(_env().get("MEDIA_PORT", "7000"))


def _url(path: str = "") -> str:
    return f"http://localhost:{_http_port()}{path}"


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def _post(path: str, payload: dict[str, object], timeout: float = 5.0) -> tuple[int, str]:
    request = urllib.request.Request(
        _url(path),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def _require_server() -> None:
    """Fail with a useful hint when the signaling plane is not up yet."""
    if not runner.port_open("127.0.0.1", _http_port()):
        runner.fail(f"no signaling plane on :{_http_port()} — start it with `make run`")
        sys.exit(1)


def _udp_reachable(host: str, port: int, timeout: float = 0.5) -> bool | None:
    """Probe a UDP port. `True` = something answered or absorbed it, `False` =
    refused, `None` = no idea.

    There is no UDP equivalent of a TCP connect, and pretending otherwise is how
    people convince themselves a media port is up when nothing is listening. The
    honest probe is: `connect` the socket (so the kernel will deliver ICMP errors
    back to us), send a byte, and see whether the *next* operation reports
    `ECONNREFUSED` — which is an ICMP port-unreachable, the only negative signal
    UDP offers. Silence means the port is open, or a firewall ate the ICMP, or
    the datagram is still in flight. `None` is the correct answer to that, and
    the same ambiguity is why V1's connectivity checks exist at all.
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
# Probes — the criteria, made visible
# --------------------------------------------------------------------------- #


@runner.task("planes", "🛰️", "Probe", "Who is up: the signaling plane and the media port")
def planes() -> None:
    """Both planes in one glance.

    Worth having because they fail independently and in this project they
    routinely do: on the bare scaffold the first STUN check kills the media pump
    while signaling keeps answering perfectly. `/readyz` is the endpoint that
    knows the difference, so it is on this table.
    """
    http_up = runner.port_open("127.0.0.1", _http_port())
    media = _udp_reachable("127.0.0.1", _media_port())

    print()
    mark = f"{C.GREEN}●{C.RESET}" if http_up else f"{C.DIM}○{C.RESET}"
    print(
        f"  {mark} signaling + admin   :{_http_port():<6} "
        f"{'listening' if http_up else 'closed':<12} {C.DIM}TCP — /rooms, /healthz{C.RESET}"
    )
    media_mark = {
        True: f"{C.GREEN}●{C.RESET}",
        False: f"{C.RED}●{C.RESET}",
        None: f"{C.YELLOW}◐{C.RESET}",
    }[media]
    media_state = {True: "answering", False: "refused", None: "no ICMP"}[media]
    print(
        f"  {media_mark} media (muxed UDP)   :{_media_port():<6} "
        f"{media_state:<12} {C.DIM}UDP — STUN/RTP/RTCP{C.RESET}"
    )
    print()
    if media is None:
        print(
            f"   {C.DIM}'no ICMP' is normal: UDP has no handshake — see `_udp_reachable`{C.RESET}"
        )
    if http_up:
        status, _ = _get(_url("/readyz"))
        if status == 200:
            runner.ok("both planes up — the media pump is forwarding")
        elif status == 503:
            runner.warn("signaling is up but the media pump is dead (a vertical raised)")
            print(
                f"   {C.DIM}check the server log for NotImplementedError"
                f" — that's the worklist{C.RESET}"
            )
    print()


@runner.task("publish", "📡", "Probe", "Create a publisher with 3 simulcast layers (V3's input)")
def publish() -> None:
    """Signaling made visible, and the shape V3 is graded against.

    Three layers at 150 kbps / 500 kbps / 2 Mbps is the canonical simulcast
    ladder the SPEC's criteria talk about: a budget just above 500 000 must
    select `h`, and a budget below 150 000 must still select `q` rather than
    nothing. The `out_ssrc` you get back from `make sub` is the other half —
    the identity V2 has to keep stable across every switch between these three.
    """
    _require_server()
    payload: dict[str, object] = {
        "layers": [
            {"rid": "q", "ssrc": 111, "bitrate_bps": 150_000},
            {"rid": "h", "ssrc": 222, "bitrate_bps": 500_000},
            {"rid": "f", "ssrc": 333, "bitrate_bps": 2_000_000},
        ],
        "client_ufrag": "probepub",
    }
    runner.step("📡", f"POST {_url('/rooms/demo/publish')}")
    status, body = _post("/rooms/demo/publish", payload)
    print()
    if status != 200:
        runner.fail(f"unexpected status {status}: {body.strip()}")
        sys.exit(1)
    handle = json.loads(body)
    print(f"  {C.BOLD}publisher {handle['peer_id']}{C.RESET}")
    print(f"    {C.CYAN}ice_ufrag{C.RESET}   {handle['ice_ufrag']}")
    print(
        f"    {C.CYAN}media_addr{C.RESET}  {handle['media_addr']}   "
        f"{C.DIM}← ICE-connect here{C.RESET}"
    )
    print(f"    {C.DIM}ice_pwd is in the response and deliberately not printed{C.RESET}")
    print()
    print(f"   {C.DIM}then: make sub PUBLISHER={handle['peer_id']}{C.RESET}")
    print()


@runner.task("sub", "👀", "Probe", "Attach a subscriber to a publisher (PUBLISHER=<id>)")
def sub() -> None:
    """The subscriber half — and the stable outbound SSRC that is V2's promise.

    That SSRC does not change for the life of the session. Not when the
    estimator drops the subscriber to the low layer, not when it climbs back to
    high, not when the SFU skips a packet under them. Everything V2 does exists
    to make that one number a lie the subscriber never catches.
    """
    _require_server()
    publisher = os.environ.get("PUBLISHER", "1")
    runner.step("👀", f"POST {_url('/rooms/demo/subscribe')}  publisher={publisher}")
    status, body = _post(
        "/rooms/demo/subscribe",
        {"publisher": int(publisher), "client_ufrag": "probesub"},
    )
    print()
    if status == 404:
        runner.fail(f"no publisher {publisher} — run `make publish` first")
        sys.exit(1)
    if status != 200:
        runner.fail(f"unexpected status {status}: {body.strip()}")
        sys.exit(1)
    handle = json.loads(body)
    print(f"  {C.BOLD}subscriber {handle['peer_id']}{C.RESET}")
    print(
        f"    {C.CYAN}out_ssrc{C.RESET}    {handle['out_ssrc']}  "
        f"{C.DIM}← stable for the whole session (V2){C.RESET}"
    )
    print(f"    {C.CYAN}media_addr{C.RESET}  {handle['media_addr']}")
    print()


@runner.task("rooms", "🗺️", "Probe", "GET /rooms — the live topology the fan-out walks")
def rooms() -> None:
    status, body = _get(_url("/rooms"))
    if status != 200:
        runner.fail(f"unexpected status {status}: {body.strip()}")
        sys.exit(1)
    parsed = json.loads(body)
    if not parsed["rooms"]:
        print(f"\n  {C.DIM}no rooms yet — `make publish`{C.RESET}\n")
        return
    print()
    for room in parsed["rooms"]:
        print(f"  {C.BOLD}{room['room']}{C.RESET}")
        for peer in room["peers"]:
            colour = C.CYAN if peer["role"] == "publisher" else C.GREEN
            print(f"    {colour}{peer['role']:<11}{C.RESET} id={peer['id']}")
    print()


@runner.task("stun", "🧊", "Probe", "Send a real STUN Binding request at the media port (V1)")
def stun() -> None:
    """V1's first criterion, run as a command rather than read as a sentence.

    This is a hand-built RFC 5389 Binding request — 20 bytes, no attributes —
    sent at the muxed media port, so it shows what the *first thing a browser
    ever sends you* actually looks like on the wire before you write the code
    that parses one.

    On the bare scaffold nothing comes back, and "nothing" is the correct
    answer to see: the datagram arrived, `classify` called it STUN, dispatch
    reached `StunMessage.parse`, and V1 raised. Check the server log — the
    traceback names the exact function to write next. Once V1 works, this prints
    a 20-byte-plus success response and you can read the XOR-MAPPED-ADDRESS out
    of it by hand.
    """
    port = _media_port()
    header = bytearray(20)
    header[0:2] = (0x0001).to_bytes(2, "big")  # class Request, method Binding
    header[2:4] = (0).to_bytes(2, "big")  # message length: no attributes
    header[4:8] = STUN_MAGIC_COOKIE.to_bytes(4, "big")
    header[8:20] = bytes(range(12))  # transaction id

    runner.step("🧊", f"sending a 20-byte Binding request to 127.0.0.1:{port}/udp")
    print(f"   {C.DIM}{bytes(header).hex(' ', 4)}{C.RESET}")
    print()
    reply = b""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(3)
            sock.connect(("127.0.0.1", port))
            sock.send(bytes(header))
            try:
                reply = sock.recv(2048)
            except (TimeoutError, ConnectionRefusedError):
                reply = b""
    except OSError as exc:
        runner.fail(f"could not send: {exc}")
        sys.exit(1)

    if not reply:
        print(f"  {C.DIM}no reply — dispatch raised before answering (V1 is a todo){C.RESET}")
        print(f"  {C.DIM}the server log has the traceback; that is the worklist{C.RESET}")
    else:
        print(f"  {C.GREEN}{len(reply)} bytes back{C.RESET}")
        print(f"    {C.DIM}{reply.hex(' ', 4)}{C.RESET}")
        if len(reply) >= 20 and reply[4:8] == STUN_MAGIC_COOKIE.to_bytes(4, "big"):
            echoed = reply[8:20] == bytes(range(12))
            print(
                f"    {C.CYAN}class/method{C.RESET}  {reply[0:2].hex()}  "
                f"{C.DIM}0101 = Binding success{C.RESET}"
            )
            print(f"    {C.CYAN}txid echoed {C.RESET}  {echoed}")
            if echoed:
                runner.ok("that is a STUN success response — V1's fourth criterion, observed")
    print()


@runner.task("status", "📊", "Probe", "GET /status — config, caps and the live graph")
def status() -> None:
    code, body = _get(_url("/status"))
    if code != 200:
        runner.fail(f"unexpected status {code}: {body.strip()}")
        sys.exit(1)
    parsed = json.loads(body)
    print()
    print(f"  {C.BOLD}media{C.RESET}      {parsed['media_addr']}")
    pump_state = "running" if parsed["media_pump_running"] else f"{C.RED}DEAD{C.RESET}"
    print(f"  {C.BOLD}pump{C.RESET}       {pump_state}")
    print(f"  {C.BOLD}dropped{C.RESET}    {parsed['media_datagrams_dropped']} datagrams")
    print(f"  {C.BOLD}limits{C.RESET}     {parsed['limits']}")
    print(f"  {C.BOLD}bitrate{C.RESET}    {parsed['bitrate_bps']}")
    print()


@runner.task("metrics", "📈", "Probe", "GET /metrics — the Prometheus scrape")
def metrics() -> None:
    _, body = _get(_url("/metrics"))
    lines = [
        ln for ln in body.splitlines() if ln and not ln.startswith("#") and ln.startswith("sfu_")
    ]
    if not lines:
        runner.warn("no sfu_* series yet — is the server running?")
        return
    for line in lines:
        print(f"  {line}")
    print()
    runner.ok(f"{len(lines)} SFU metric series")


@runner.task("smoke", "🔥", "Run", "Hit /healthz on HTTP_PORT (server must be running)")
def smoke() -> None:
    runner.require("curl", "Install curl to use this target.")
    # This project uses HTTP_PORT (not the generic PORT) — MEDIA_PORT is the other one.
    runner.step("🔥", f"GET {_url('/healthz')}")
    rc = runner.run(["curl", "-sf", _url("/healthz")], check=False)
    print()
    if rc == 0:
        runner.ok("healthz OK")
    else:
        runner.fail("healthz failed — is the server running?")
        sys.exit(1)


@runner.task("bench", "🐉", "Bench", "The Crowded Room: one publisher, 50+ degraded subscribers")
def bench() -> None:
    """The boss fight's load generator.

    Not implemented for you — building it *is* part of the fight, and the SPEC's
    Arena line says what it has to do: one publisher at ~1.5 Mbps across three
    simulcast layers, at least 50 subscribers that ICE-connect and receive, on a
    spread of `tc netem` downlink profiles including one that sags to 25% for
    60 s and recovers, for at least five minutes.

    Three things worth deciding before you write a line of it. The harness must
    not be the bottleneck — fifty subscribers in one Python process share one
    GIL with each other *and* would be measuring your GIL against theirs, so the
    honest shapes are separate processes or containers. Quality has to be
    measured from the subscribers' received streams and the SFU's own metrics,
    not from vibes: the continuity criterion is a claim about every sequence
    number each subscriber saw. And the convergence criterion is a *time* — 3 s
    from joining — so the harness has to record when each subscriber joined,
    not just what layer it ended on.
    """
    runner.warn("bench/ is yours to build — see the 🐉 Boss fight section of SPEC.md")
    print(f"   {C.DIM}make md   # read the Arena + 'the boss falls when' lines{C.RESET}")
    print(
        f"   {C.DIM}while it runs: watch "
        f"`make metrics | grep -E 'forwarded|dropped|switches'`{C.RESET}"
    )
    print(f"   {C.DIM}sudo tc qdisc add dev lo root netem rate 600kbit delay 40ms 10ms{C.RESET}")


@runner.task("profile", "🔥", "Bench", "Sample the running SFU with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    On CPython the boss fight is won or lost in the profile. py-spy attaches to
    a *running* process by PID, so start the SFU, drive the fan-out at it from
    another shell, and sample while that is happening — a flamegraph of an idle
    event loop tells you nothing at all.

    What to look for is named in `main.py`: the per-subscriber `bytearray` copy,
    the fifty synchronous `sendto` calls, and anything blocking the loop.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    pid = os.environ.get("PID")
    if not pid:
        runner.fail("set PID=<sfu pid> — py-spy samples a running process")
        print(f"   {C.DIM}e.g. `PID=$(pgrep -f webrtc-sfu) make profile`{C.RESET}")
        sys.exit(1)
    runner.step("🔥", "sampling for 10s — drive the fan-out meanwhile")
    runner.uv("run", "py-spy", "record", "--duration", "10", "--pid", pid, "--output", str(out))
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
