#!/usr/bin/env python3
"""bittorrent — local dev task runner.

A wrapper around the day-to-day commands for this project (uv, docker, and the
probes that make a *peer-to-peer* client visible — which matters more here than
in most projects, because almost nothing interesting happens over HTTP). The
`Makefile` shells out to this file so there is one source of truth with colors,
emojis and readable output. Help tables use `tools/makefile_help.py` (Rich —
auto-installed from `tools/requirements.txt`).

The dev loop: **your client runs locally** (`make run` → control plane on :8080,
peer port on :6819) while the **test swarm runs in Docker** (`make up` → an
opentracker on :6919 speaking both HTTP and UDP, and a `transmission` reference
peer on :51419). That split is the entire point of the compose file: a tracker
to announce to and a strict, spec-compliant peer that will drop you the moment
your handshake or framing is wrong — which is how you find out that it is.

The probe tasks are the reason this file exists:

* `make swarm`    — the test swarm made visible: is the tracker up, is the
                    reference peer up, is your peer port actually accepting?
* `make announce` — V3 made visible: ask the compose tracker who has an
                    infohash, over HTTP, with `curl`. No client needed.
* `make torrent`  — make a real `.torrent` for a real file, pointed at the
                    compose tracker. The corpus V1 and V2 are graded against.
* `make add`      — V2 made visible: POST that torrent at your control plane.
* `make handshake`— V4 made visible: send the 68 bytes to your seeder and show
                    exactly what came back.
* `make bench`    — the boss fight's load generator: N leechers at one seeder.

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
import urllib.parse
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
    register_smoke_healthz,
)

TRACKER_PORT = 6919
"""The compose tracker's host port — opentracker's 6969 with the last two digits
replaced by the project number, per the repo-wide rule. It serves HTTP *and* UDP
on the same port, which is exactly the two transports V3 implements."""

REFERENCE_RPC_PORT = 9019
"""The transmission reference peer's web UI / RPC port (9091 → NN=19)."""

REFERENCE_PEER_PORT = 51419
"""The reference peer's BitTorrent port (51413 → NN=19). This is the address you
dial in V4 to have a real, unforgiving client judge your handshake."""

runner = make_runner(
    crate="bittorrent",
    help_title="🌊  bittorrent",
    project_dir=PROJECT_DIR,
    # The control plane's port: `make smoke` probes /healthz, which lives on
    # HTTP, not on the peer wire.
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make up && make run"),
        ("See the swarm", "make swarm"),
        ("Make a test torrent", "make torrent  (then: make add)"),
        ("Talk to the tracker (V3)", "make announce"),
        ("Poke your seeder (V4)", "make handshake"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_compose_lifecycle(runner)
run_server = register_python_run(runner)
register_smoke_healthz(runner)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


def _env() -> dict[str, str]:
    return runner.load_dotenv()


def _peer_port() -> int:
    return int(_env().get("PEER_PORT", "6819"))


def _http_url(path: str = "") -> str:
    port = _env().get("PORT", runner.config.default_port)
    return f"http://localhost:{port}{path}"


def _download_dir() -> Path:
    raw = _env().get("DOWNLOAD_DIR", "./data")
    path = Path(raw)
    return path if path.is_absolute() else (PROJECT_DIR / path)


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def _require_server() -> None:
    """Fail with a useful hint when the control plane isn't up yet."""
    port = int(_env().get("PORT", runner.config.default_port))
    if not runner.port_open("127.0.0.1", port):
        runner.fail(f"no control plane on :{port} — start it with `make run`")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Services — the test swarm
# --------------------------------------------------------------------------- #


@runner.task("up", "🐳", "Services", f"Start the test swarm (tracker :{TRACKER_PORT})")
def up() -> None:
    runner.step("🐳", "starting the tracker + reference peer…")
    runner.run([*runner.compose, "up", "-d"], cwd=runner.project_dir)
    print(
        f"   {C.DIM}tracker: http://localhost:{TRACKER_PORT}/announce  ·  "
        f"udp://localhost:{TRACKER_PORT}  ·  YOUR client is `make run`{C.RESET}"
    )


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("dev", "🚀", "Run", "sync + start the test swarm + run your client")
def dev() -> None:
    sync()
    up()
    run_server()


# --------------------------------------------------------------------------- #
# Probes — the criteria, made visible
# --------------------------------------------------------------------------- #


@runner.task("swarm", "🌊", "Probe", "Who is up: tracker, reference peer, your peer port")
def swarm() -> None:
    """The whole environment in one glance.

    Worth having because a peer-to-peer client fails silently in a way a server
    does not: "no peers" looks identical whether the tracker is down, the
    reference peer never started, or your announce is malformed. This separates
    the first two from the third before you go debugging the third.
    """
    rows = [
        ("tracker (HTTP)", "127.0.0.1", TRACKER_PORT, "V3 announces here"),
        ("reference peer", "127.0.0.1", REFERENCE_PEER_PORT, "V4/V5 talk to this"),
        ("reference RPC", "127.0.0.1", REFERENCE_RPC_PORT, "transmission web UI"),
        ("your peer port", "127.0.0.1", _peer_port(), "V6 accepts here"),
        (
            "your control plane",
            "127.0.0.1",
            int(_env().get("PORT", runner.config.default_port)),
            "POST /torrents",
        ),
    ]
    print()
    for label, host, port, note in rows:
        open_ = runner.port_open(host, port)
        mark = f"{C.GREEN}●{C.RESET}" if open_ else f"{C.DIM}○{C.RESET}"
        state = "listening" if open_ else "closed"
        print(f"  {mark} {label:<20} :{port:<6} {state:<10} {C.DIM}{note}{C.RESET}")
    print()
    if not runner.port_open("127.0.0.1", _peer_port()):
        print(f"   {C.DIM}your peer port is closed — set RUN_SEEDER=true to seed (V6){C.RESET}")
    print()


@runner.task("announce", "📡", "Probe", "Ask the compose tracker who has an infohash (V3, HTTP)")
def announce() -> None:
    """V3's first criterion, run as a command rather than read as a sentence.

    This is a hand-built announce with `curl` — no client involved — so it shows
    you what a *correct* one looks like on the wire before you write the code
    that produces one. Note the `info_hash` parameter: 20 raw bytes,
    percent-encoded byte by byte. That is the encoding step that eats a day, and
    seeing it spelled out here is cheaper than deriving it from a tracker's
    error message.
    """
    if not runner.port_open("127.0.0.1", TRACKER_PORT):
        runner.fail(f"no tracker on :{TRACKER_PORT} — run `make up`")
        sys.exit(1)
    runner.require("curl", "Install curl to use this target.")

    info_hash = bytes(range(20))
    peer_id = b"-PB0001-" + b"0123456789ab"
    query = urllib.parse.urlencode(
        {
            "info_hash": info_hash,
            "peer_id": peer_id,
            "port": _peer_port(),
            "uploaded": 0,
            "downloaded": 0,
            "left": 0,
            "compact": 1,
            "event": "started",
        },
        quote_via=urllib.parse.quote_from_bytes,
        safe="",
    )
    url = f"http://localhost:{TRACKER_PORT}/announce?{query}"
    runner.step("📡", "GET /announce with a percent-encoded raw infohash")
    print(f"   {C.DIM}{url}{C.RESET}")
    print()
    runner.run(["curl", "-sS", "--max-time", "10", url], check=False)
    print()
    print(
        f"   {C.DIM}that reply is bencoded — 'd8:intervali…' — which is what V1 "
        f"decodes and V3 reads `peers` out of{C.RESET}"
    )


@runner.task("torrent", "🧲", "Probe", "Build a real .torrent for a test file (the V1/V2 corpus)")
def torrent() -> None:
    """Make the corpus the SPEC keeps referring to.

    V1 and V2 are both graded against "a checked-in real `.torrent`", and this
    produces one — bencoded by hand, pointed at the compose tracker, with a
    payload big enough to span several pieces so the piece table is not a
    degenerate single entry.

    Hand-rolling the bencoding here is deliberate: it is about thirty lines, it
    is the format you are about to implement, and reading it once from the
    *producer* side makes the decoder's rules concrete. It also means the
    infohash printed below was computed by code that is not yours, so when your
    V2 disagrees with it, one of you is wrong in a way you can actually chase.
    """
    import hashlib

    data_dir = _download_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = data_dir / "testfile.bin"
    piece_length = 16384
    size = piece_length * 5 + 137  # a short final piece, on purpose

    if not payload.exists():
        payload.write_bytes(bytes((i * 7 + 13) % 256 for i in range(size)))
    raw = payload.read_bytes()

    pieces = b"".join(
        hashlib.sha1(raw[i : i + piece_length], usedforsecurity=False).digest()
        for i in range(0, len(raw), piece_length)
    )

    def ben(value: object) -> bytes:
        match value:
            case bool():
                raise TypeError("bencode has no boolean")
            case int():
                return b"i%de" % value
            case bytes():
                return b"%d:%s" % (len(value), value)
            case str():
                return ben(value.encode())
            case list():
                return b"l" + b"".join(ben(v) for v in value) + b"e"  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            case dict():
                items = sorted(value.items())  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                return b"d" + b"".join(ben(k) + ben(v) for k, v in items) + b"e"
            case _:
                raise TypeError(f"cannot bencode {type(value).__name__}")

    info = {
        b"name": payload.name.encode(),
        b"piece length": piece_length,
        b"pieces": pieces,
        b"length": len(raw),
    }
    info_bytes = ben(info)
    meta = {
        b"announce": f"http://localhost:{TRACKER_PORT}/announce".encode(),
        b"announce-list": [
            [f"http://localhost:{TRACKER_PORT}/announce".encode()],
            [f"udp://localhost:{TRACKER_PORT}".encode()],
        ],
        b"info": info,
    }
    out = PROJECT_DIR / "testfile.torrent"
    out.write_bytes(ben(meta))

    info_hash = hashlib.sha1(info_bytes, usedforsecurity=False).hexdigest()
    print()
    print(f"  {C.BOLD}{out.name}{C.RESET}")
    print(f"    payload      {payload}  ({len(raw):,} bytes)")
    print(f"    piece length {piece_length:,}   pieces {len(pieces) // 20}")
    print(f"    {C.CYAN}infohash{C.RESET}     {info_hash}")
    print()
    print(f"   {C.DIM}that infohash is what your V2 must reproduce, byte for byte{C.RESET}")
    print(f"   {C.DIM}magnet:?xt=urn:btih:{info_hash}{C.RESET}")
    print()
    runner.ok("wrote the test torrent — now `make add`")


@runner.task("add", "➕", "Probe", "POST the test torrent at your control plane (V2)")
def add() -> None:
    """V2 made visible.

    On the bare scaffold this fails at the metainfo parser, and the *way* it
    fails is informative: the control plane accepted the body (so the wiring
    works) and then raised inside `Metainfo.from_bytes` (so V2 is what is
    missing). That is the difference between "nothing works" and "one named
    function is unwritten".
    """
    _require_server()
    torrent_file = PROJECT_DIR / "testfile.torrent"
    if not torrent_file.exists():
        runner.warn("no testfile.torrent — running `make torrent` first")
        torrent()
    runner.require("curl", "Install curl to use this target.")
    runner.step("➕", f"POST {_http_url('/torrents')}")
    rc = runner.run(
        [
            "curl",
            "-sS",
            "-i",
            "-X",
            "POST",
            "--data-binary",
            f"@{torrent_file}",
            "-H",
            "Content-Type: application/octet-stream",
            _http_url("/torrents"),
        ],
        check=False,
    )
    print()
    if rc != 0:
        runner.fail("the control plane did not answer")
        sys.exit(1)
    print(f"   {C.DIM}then: make torrents{C.RESET}")


@runner.task("torrents", "📋", "Probe", "GET /torrents — what the client is managing")
def torrents() -> None:
    status, body = _get(_http_url("/torrents"))
    if status != 200:
        runner.fail(f"unexpected status {status}: {body.strip()}")
        sys.exit(1)
    parsed = json.loads(body)
    if not parsed:
        print(f"\n  {C.DIM}no torrents yet — `make add`{C.RESET}\n")
        return
    print()
    for entry in parsed:
        have, total = entry["have_pieces"], entry["total_pieces"]
        pct = (100 * have / total) if total else 0.0
        print(f"  {C.BOLD}{entry['name']}{C.RESET}")
        print(f"    {C.CYAN}{entry['info_hash']}{C.RESET}")
        print(
            f"    {have}/{total} pieces ({pct:.1f}%)  "
            f"down {entry['downloaded']:,}  up {entry['uploaded']:,}  "
            f"peers {entry['peers']}"
        )
    print()


@runner.task("handshake", "🤝", "Probe", "Send the 68-byte handshake to your seeder (V4/V6)")
def handshake() -> None:
    """V4 and V6 made visible, from the other side of the wire.

    Sends a well-formed 68-byte handshake at your peer port and prints exactly
    what came back. On the scaffold that is nothing — the session raises before
    it reads a byte — and "nothing" is the correct answer to see, because it
    means the accept loop is real and only `serve_peer` is missing.

    `transmission` in the compose file will do this to you far less politely.
    """
    port = _peer_port()
    if not runner.port_open("127.0.0.1", port):
        runner.fail(f"nothing listening on :{port} — set RUN_SEEDER=true and `make run`")
        sys.exit(1)

    frame = (
        bytes([19])
        + b"BitTorrent protocol"
        + bytes(8)  # reserved: extension flags, and never a reason to reject
        + bytes(range(20))  # a made-up infohash — a real seeder should drop us for it
        + b"-PB0001-probeprobe"
    )
    runner.step("🤝", f"sending 68 bytes to 127.0.0.1:{port}")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(frame)
        sock.settimeout(3)
        try:
            reply = sock.recv(4096)
        except (TimeoutError, ConnectionResetError):
            reply = b""

    print()
    if not reply:
        print(f"  {C.DIM}no reply — the session raised before answering (V6 is a todo){C.RESET}")
    else:
        print(f"  {C.GREEN}{len(reply)} bytes back{C.RESET}")
        print(f"    {C.DIM}{reply[:68].hex(' ', 4)}{C.RESET}")
        if len(reply) >= 68 and reply[0] == 19 and reply[1:20] == b"BitTorrent protocol":
            print(f"    {C.CYAN}infohash{C.RESET}  {reply[28:48].hex()}")
            print(f"    {C.CYAN}peer id {C.RESET}  {reply[48:68]!r}")
            runner.ok("that is a valid handshake — V4's first criterion, observed")
    print()


@runner.task("metrics", "📈", "Probe", "GET /metrics — the Prometheus scrape")
def metrics() -> None:
    _, body = _get(_http_url("/metrics"))
    lines = [
        ln for ln in body.splitlines() if ln and not ln.startswith("#") and ln.startswith("bt_")
    ]
    if not lines:
        runner.warn("no bt_* series yet — the metric call sites are the observability horizontal")
        return
    for line in lines:
        print(f"  {line}")
    print()
    runner.ok(f"{len(lines)} client metric series")


@runner.task("data", "💾", "Probe", "What's actually on disk in the download dir")
def data() -> None:
    """A BitTorrent client's real state is files, and files are the one thing you
    cannot inspect over a socket. Sizes that never grow mean pieces are not being
    written (V5); a file that is full-size immediately is the sparse
    preallocation, which is correct and looks alarming the first time."""
    directory = _download_dir()
    if not directory.exists():
        runner.warn(f"{directory} does not exist yet — run the client once")
        return
    entries = sorted(p for p in directory.rglob("*") if p.is_file())
    print()
    print(f"  {C.BOLD}{directory}{C.RESET}")
    if not entries:
        print(f"    {C.DIM}empty — nothing downloaded yet (V5){C.RESET}")
    for path in entries:
        stat = path.stat()
        # st_blocks is 512-byte units; a sparse file reports far fewer than its
        # apparent size, which is how you tell "preallocated" from "downloaded".
        actual = stat.st_blocks * 512
        rel = path.relative_to(directory)
        print(
            f"    {C.GREEN}{str(rel):<28}{C.RESET}{stat.st_size:>12,} bytes  "
            f"{C.DIM}({actual:,} on disk){C.RESET}"
        )
    print()


@runner.task("bench", "🐉", "Bench", "The Flash Crowd: N leechers against your seeder")
def bench() -> None:
    """The boss fight's load generator.

    Not implemented for you — building it *is* part of the fight, and the SPEC's
    Arena line says what it has to do: spin up ≥ 50 concurrent leechers all
    fetching the same torrent from your one seeder, and measure completion, the
    instantaneous unchoke count, aggregate throughput, and the seeder's RSS.

    Two things worth deciding before you write a line of it. The harness must
    not be the bottleneck — 50 leechers in one Python process share one GIL with
    each other, so the honest shapes are separate processes or containers of a
    real client. And it has to sample `bt_peers_unchoked` *during* the storm
    rather than after: the criterion is "at any instant", and a reading taken
    once the flood has drained proves nothing.
    """
    runner.warn("bench/ is yours to build — see the 🐉 Boss fight section of SPEC.md")
    print(f"   {C.DIM}make md   # read the SPEC's Arena + 'boss falls when' lines{C.RESET}")
    print(f"   {C.DIM}while it runs: watch `make metrics | grep unchoked`{C.RESET}")


@runner.task("profile", "🔥", "Bench", "Sample the running client with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    On CPython the boss fight is won or lost in the profile. py-spy attaches to a
    *running* process by PID, so start the client, drive load at it in another
    shell, and sample while that is happening — a flamegraph of an idle event
    loop tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    pid = os.environ.get("PID")
    if not pid:
        runner.fail("set PID=<client pid> — py-spy samples a running process")
        print(f"   {C.DIM}e.g. `PID=$(pgrep -f bittorrent) make profile`{C.RESET}")
        sys.exit(1)
    runner.step("🔥", "sampling for 10s — drive load meanwhile")
    runner.uv("run", "py-spy", "record", "--duration", "10", "--pid", pid, "--output", str(out))
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
