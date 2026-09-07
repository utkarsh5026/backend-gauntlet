#!/usr/bin/env python3
"""media-transport — local dev task runner.

A wrapper around the day-to-day commands for this project (uv, the server, and
the probes that make a *media* transport visible — which matters more here than
in most projects, because almost nothing interesting happens over HTTP). The
`Makefile` shells out to this file so there is one source of truth with colors,
emojis and readable output. Help tables use `tools/makefile_help.py` (Rich —
auto-installed from `tools/requirements.txt`).

There is deliberately **no docker-compose** here: there is no database and no
broker. The media plane is a raw UDP socket and everything else — the
packetizer, the jitter buffer, the retransmit cache, the estimator — lives
in-process. So this runner has no compose/db bundles, just checks, run, and the
probes.

The probe tasks are the reason this file exists. A media transport fails
silently in a way a CRUD service does not: "the video is bad" looks identical
whether nothing is arriving, the jitter buffer is stalling, the NACK loop never
fires, or the estimator has parked at the floor. These separate those from each
other, in that order:

* `make planes` — is anything even up? The admin plane is TCP and easy; the RTP
                  port is UDP and needs an actual probe (see `_udp_reachable`).
* `make send`   — V1 made visible: send a real, hand-built RTP datagram at the
                  media port and watch what the receiver does with it.
* `make pair`   — the loopback smoke test from SPEC.md's "Run it", as one
                  command: a receiver and a sender pointed at each other.
* `make status` — role, bounds, and whether the session task is still alive.
* `make netem`  — the impairments the boss fight runs behind, printed with the
                  matching teardown so you do not leave `lo` degraded.
* `make bench`  — the Lossy Mile's harness (yours to build).

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
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
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
)

RTP_MIN_HEADER = 12
"""Spelled out here so `make send` can build a real datagram without importing
the package it is probing — a probe that depends on the code under test tells
you nothing about the code under test."""

runner = make_runner(
    crate="media-transport",
    help_title="📡 media-transport (RTP · jitter buffer · NACK · congestion control)",
    project_dir=PROJECT_DIR,
    # The admin port. The media plane is RTP_PORT, and it is UDP.
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("See what is up", "make planes"),
        ("Poke the media port (V1)", "make send"),
        ("Loopback sender → receiver", "make pair"),
        ("Degrade the link (boss)", "make netem  /  make netem-off"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_python_run(runner)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


def _env() -> dict[str, str]:
    return runner.load_dotenv()


def _http_port() -> int:
    return int(_env().get("HTTP_PORT", runner.config.default_port))


def _rtp_port() -> int:
    return int(_env().get("RTP_PORT", "5004"))


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


def _udp_reachable(host: str, port: int, timeout: float = 0.5) -> bool | None:
    """Probe a UDP port. `True` = something absorbed it, `False` = refused,
    `None` = no idea.

    There is no UDP equivalent of a TCP connect, and pretending otherwise is how
    people convince themselves a media port is up when nothing is listening. The
    honest probe is: `connect` the socket (so the kernel delivers ICMP errors
    back to us), send a byte, and see whether the next operation reports
    `ECONNREFUSED` — an ICMP port-unreachable, the only negative signal UDP
    offers. Silence means the port is open, or a firewall ate the ICMP, or the
    datagram is still in flight. `None` is the correct answer to that, and the
    same ambiguity is why RTCP feedback exists at all: on UDP, the only way to
    know your packets arrived is for the far end to tell you.
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


def _rtp_datagram(sequence: int = 1, timestamp: int = 0, ssrc: int = 0xDEADBEEF) -> bytes:
    """A minimal well-formed RTP datagram: version 2, PT 96, 8 bytes of payload."""
    buf = bytearray(RTP_MIN_HEADER + 8)
    buf[0] = 0x80  # version 2, no padding, no extension, no CSRCs
    buf[1] = 0x80 | 96  # marker set, dynamic payload type 96
    buf[2:4] = sequence.to_bytes(2, "big")
    buf[4:8] = timestamp.to_bytes(4, "big")
    buf[8:12] = ssrc.to_bytes(4, "big")
    return bytes(buf)


# --------------------------------------------------------------------------- #
# Probes — the criteria, made visible
# --------------------------------------------------------------------------- #


@runner.task("planes", "📡", "Probe", "Who is up: the admin plane and the RTP port")
def planes() -> None:
    """Both planes in one glance.

    Worth having because they fail independently and in this project they
    routinely do: on the bare scaffold the first datagram kills the media
    session while the admin server keeps answering perfectly. `/readyz` is the
    endpoint that knows the difference, so it is on this table.
    """
    http_up = runner.port_open("127.0.0.1", _http_port())
    media = _udp_reachable("127.0.0.1", _rtp_port())

    print()
    mark = f"{C.GREEN}●{C.RESET}" if http_up else f"{C.DIM}○{C.RESET}"
    print(
        f"  {mark} admin              :{_http_port():<6} "
        f"{'listening' if http_up else 'closed':<12} {C.DIM}TCP — /healthz, /metrics{C.RESET}"
    )
    media_mark = {
        True: f"{C.GREEN}●{C.RESET}",
        False: f"{C.RED}●{C.RESET}",
        None: f"{C.YELLOW}◐{C.RESET}",
    }[media]
    media_state = {True: "answering", False: "refused", None: "no ICMP"}[media]
    print(
        f"  {media_mark} media (RTP/RTCP)   :{_rtp_port():<6} "
        f"{media_state:<12} {C.DIM}UDP — the media plane{C.RESET}"
    )
    print()
    if media is None:
        print(
            f"   {C.DIM}'no ICMP' is normal: UDP has no handshake — see `_udp_reachable`{C.RESET}"
        )
    if http_up:
        status_code, _ = _get(_url("/readyz"))
        if status_code == 200:
            runner.ok("both planes up — the media session is running")
        elif status_code == 503:
            runner.warn("admin is up but the media session is dead (a vertical raised)")
            print(f"   {C.DIM}make status   # the exception, without digging in the log{C.RESET}")
    print()


@runner.task("send", "📨", "Probe", "Send a real RTP datagram at the media port (V1)")
def send() -> None:
    """V1's first criterion, run as a command rather than read as a sentence.

    This is a hand-built RTP packet — 12 bytes of header, marker set, payload
    type 96 — sent at the media port, so it shows what *the thing your parser
    will actually be handed* looks like on the wire before you write the code
    that parses one.

    On the bare scaffold nothing comes back, and "nothing" is the correct answer
    to see: the datagram arrived, the receive loop reached `RtpPacket.parse`,
    and V1 raised. Check `make status` — the exception names the exact function
    to write next. Once V1 and V2 work, this packet is admitted to the jitter
    buffer and you will see `media_transport_jitter_buffer_depth` move.
    """
    port = _rtp_port()
    datagram = _rtp_datagram()
    runner.step("📨", f"sending a {len(datagram)}-byte RTP packet to 127.0.0.1:{port}/udp")
    print(f"   {C.DIM}{datagram.hex(' ', 4)}{C.RESET}")
    print(
        f"   {C.DIM}    ^^ 80 = v2/no CSRC   e0 = marker|PT 96   "
        f"seq 0001   ts 00000000   ssrc deadbeef{C.RESET}"
    )
    print()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.connect(("127.0.0.1", port))
            sock.send(datagram)
            try:
                reply = sock.recv(2048)
            except (TimeoutError, ConnectionRefusedError):
                reply = b""
    except OSError as exc:
        runner.fail(f"could not send: {exc}")
        sys.exit(1)

    if reply:
        print(f"  {C.GREEN}{len(reply)} bytes back{C.RESET}  {C.DIM}(RTCP feedback){C.RESET}")
        print(f"    {C.DIM}{reply.hex(' ', 4)}{C.RESET}")
    else:
        print(f"  {C.DIM}nothing came back — expected on the bare scaffold{C.RESET}")
    print()
    if runner.port_open("127.0.0.1", _http_port()):
        code, body = _get(_url("/status"))
        if code == 200:
            parsed = json.loads(body)
            if parsed["session_error"]:
                runner.warn(f"session ended: {parsed['session_error']}")
                print(f"   {C.DIM}that is the worklist{C.RESET}")
            else:
                runner.ok("session still running — the datagram was handled")
            print(f"   {C.DIM}received: see `make metrics | grep received`{C.RESET}")
    print()


@runner.task("pair", "🔁", "Run", "Loopback: a receiver and a sender pointed at each other")
def pair() -> None:
    """SPEC.md's two-terminal smoke test, as one command.

    A receiver on `RTP_PORT` and a sender on `RTP_PORT + 1` aimed at it, each
    with its own admin port so both `/metrics` are scrapeable. This is the
    clean-link setup the suggested order of attack asks for at step 3: get
    smooth playout working over localhost with no impairment at all before you
    turn on `tc netem` and start debugging two things at once.

    Ctrl-C stops both. On the bare scaffold both sides die within a frame — the
    sender at `packetize`, the receiver at `parse` — which is a useful thing to
    watch happen.
    """
    env = os.environ.copy()
    env.update(_env())
    rtp = _rtp_port()
    http = _http_port()

    receiver_env = env | {
        "ROLE": "receiver",
        "RTP_PORT": str(rtp),
        "HTTP_PORT": str(http),
    }
    sender_env = env | {
        "ROLE": "sender",
        "RTP_PORT": str(rtp + 1),
        "HTTP_PORT": str(http + 1),
        "REMOTE_ADDR": f"127.0.0.1:{rtp}",
    }

    runner.step("🔁", f"receiver on :{rtp}/udp (admin :{http})")
    print(f"   {C.DIM}sender   on :{rtp + 1}/udp (admin :{http + 1}) → 127.0.0.1:{rtp}{C.RESET}")
    print(f"   {C.DIM}Ctrl-C stops both{C.RESET}")
    print()

    command = ["uv", "run", runner.crate]
    processes: list[subprocess.Popen[bytes]] = []
    try:
        processes.append(subprocess.Popen(command, cwd=runner.project_dir, env=receiver_env))
        processes.append(subprocess.Popen(command, cwd=runner.project_dir, env=sender_env))
        for process in processes:
            process.wait()
    except KeyboardInterrupt:
        print()
        runner.warn("stopping both sides")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


@runner.task("status", "📊", "Probe", "GET /status — role, bounds, and the session's health")
def status() -> None:
    code, body = _get(_url("/status"))
    if code != 200:
        runner.fail(f"unexpected status {code}: {body.strip()}")
        sys.exit(1)
    parsed = json.loads(body)
    print()
    print(f"  {C.BOLD}role{C.RESET}       {parsed['role']}")
    print(f"  {C.BOLD}rtp{C.RESET}        {parsed['rtp_addr']}  {C.DIM}udp{C.RESET}")
    if parsed["remote_addr"]:
        print(f"  {C.BOLD}remote{C.RESET}     {parsed['remote_addr']}")
    session_state = "running" if parsed["session_running"] else f"{C.RED}DEAD{C.RESET}"
    print(f"  {C.BOLD}session{C.RESET}    {session_state}")
    if parsed["session_error"]:
        print(f"  {C.BOLD}error{C.RESET}      {C.YELLOW}{parsed['session_error']}{C.RESET}")
    print(f"  {C.BOLD}dropped{C.RESET}    {parsed['datagrams_dropped']} datagrams")
    print(
        f"  {C.BOLD}mtu{C.RESET}        {parsed['mtu']}  {C.DIM}payload type {C.RESET}"
        f"{parsed['payload_type']}"
    )
    print(f"  {C.BOLD}playout{C.RESET}    {parsed['playout_ms']} ms")
    print(f"  {C.BOLD}bounds{C.RESET}     {parsed['bounds']}")
    print(f"  {C.BOLD}bitrate{C.RESET}    {parsed['bitrate_bps']}")
    print()


@runner.task("metrics", "📈", "Probe", "GET /metrics — the Prometheus scrape")
def metrics() -> None:
    _, body = _get(_url("/metrics"))
    lines = [
        line
        for line in body.splitlines()
        if line and not line.startswith("#") and line.startswith("media_transport_")
    ]
    if not lines:
        runner.warn("no media_transport_* series yet — is the server running?")
        return
    for line in lines:
        print(f"  {line}")
    print()
    runner.ok(f"{len(lines)} transport metric series")


@runner.task("smoke", "🔥", "Run", "Hit /healthz on HTTP_PORT (server must be running)")
def smoke() -> None:
    runner.require("curl", "Install curl to use this target.")
    # This project uses HTTP_PORT (not the generic PORT) — RTP_PORT is the other.
    runner.step("🔥", f"GET {_url('/healthz')}")
    rc = runner.run(["curl", "-sf", _url("/healthz")], check=False)
    print()
    if rc == 0:
        runner.ok("healthz OK")
    else:
        runner.fail("healthz failed — is the server running?")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# The boss fight
# --------------------------------------------------------------------------- #


@runner.task("netem", "🌩️", "Bench", "Print the tc netem commands for the Lossy Mile")
def netem() -> None:
    """The Arena's impairments, printed rather than run.

    Printed and not executed on purpose. These need root, they change the
    behaviour of `lo` for **every** process on the machine, and a forgotten
    `netem` on loopback is an afternoon of debugging something entirely
    unrelated. Read them, run them yourself, and note the teardown before you
    run the setup.
    """
    print()
    print(f"  {C.BOLD}the degraded link{C.RESET}  {C.DIM}(5% loss, 30ms ± 10ms, reorder){C.RESET}")
    print(
        f"    {C.CYAN}sudo tc qdisc add dev lo root netem "
        f"loss 5% delay 30ms 10ms reorder 25% 50%{C.RESET}"
    )
    print()
    print(f"  {C.BOLD}the bandwidth cap{C.RESET}   {C.DIM}(and the mid-run drop to 50%){C.RESET}")
    print(f"    {C.CYAN}sudo tc qdisc change dev lo root netem ... rate 1500kbit{C.RESET}")
    print(f"    {C.CYAN}sudo tc qdisc change dev lo root netem ... rate 750kbit{C.RESET}")
    print(f"    {C.DIM}...wait 60s, then put it back — that is the recovery criterion{C.RESET}")
    print()
    print(
        f"  {C.BOLD}teardown{C.RESET}           {C.DIM}(run this first if anything is odd){C.RESET}"
    )
    print(f"    {C.CYAN}sudo tc qdisc del dev lo root{C.RESET}")
    print()
    print(f"   {C.DIM}make netem-off runs the teardown for you{C.RESET}")
    print()


@runner.task("netem-off", "🌤️", "Bench", "Remove any netem qdisc from lo (needs sudo)")
def netem_off() -> None:
    runner.require("tc", "Install iproute2 to use this target.")
    runner.step("🌤️", "removing the netem qdisc from lo")
    rc = runner.run(["sudo", "tc", "qdisc", "del", "dev", "lo", "root"], check=False)
    if rc == 0:
        runner.ok("lo is clean")
    else:
        runner.warn("nothing to remove (or no sudo) — that is usually fine")


@runner.task("bench", "🐉", "Bench", "The Lossy Mile: 5 minutes on a degraded, sagging link")
def bench() -> None:
    """The boss fight's harness.

    Not implemented for you — building it *is* part of the fight, and the SPEC's
    Arena line says what it has to do: a sender and a receiver at ~1.5 Mbps with
    `tc netem` between them injecting 5% loss, 30 ms ± 10 ms jitter and reorder,
    plus a bandwidth cap that drops to 50% for 60 s partway through, for at
    least five minutes.

    Three things worth deciding before you write a line of it. Quality is
    measured from the **receiver's playout timeline**, not from vibes: "99.5% of
    frames played on time" is a claim about every frame's release moment, so the
    receiver has to record them. Effective-versus-raw loss needs both numbers,
    which means the harness has to know what the impairment dropped as well as
    what the transport recovered — count at the sender, not just at the
    receiver. And "RSS stays flat" is a five-minute claim, so sample it
    throughout rather than reading it once at the end.
    """
    runner.warn("bench/ is yours to build — see the 🐉 Boss fight section of SPEC.md")
    print(f"   {C.DIM}make md      # read the Arena + 'the boss falls when' lines{C.RESET}")
    print(f"   {C.DIM}make netem   # the impairments it runs behind{C.RESET}")
    print(
        f"   {C.DIM}while it runs: watch "
        f"`make metrics | grep -E 'lost|nacks|retransmit|bitrate'`{C.RESET}"
    )


@runner.task("profile", "🔥", "Bench", "Sample the running transport with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    On CPython the boss fight is won or lost in the profile. py-spy attaches to
    a *running* process by PID, so start the transport, drive real traffic at it
    from another shell, and sample while that is happening — a flamegraph of an
    idle event loop tells you nothing at all.

    What to look for is named in `main.py`: per-packet allocation through the
    GC, the 10 ms playout tick's wakeup latency, and anything holding the one
    thread that also runs the HTTP server.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    pid = os.environ.get("PID")
    if not pid:
        runner.fail("set PID=<transport pid> — py-spy samples a running process")
        print(f"   {C.DIM}e.g. `PID=$(pgrep -f media-transport) make profile`{C.RESET}")
        sys.exit(1)
    runner.step("🔥", "sampling for 10s — drive traffic meanwhile")
    runner.uv("run", "py-spy", "record", "--duration", "10", "--pid", pid, "--output", str(out))
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
