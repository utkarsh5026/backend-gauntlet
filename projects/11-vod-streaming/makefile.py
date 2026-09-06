#!/usr/bin/env python3
"""vod-streaming — local dev task runner.

A wrapper around the day-to-day commands for this project (uv, ffmpeg, and the
probes that make the packager's behaviour *visible*: what the playlist says, what
a `Range` request actually gets back, whether `init + seg` is a file a real
decoder accepts). The `Makefile` shells out to this file so there is one source
of truth with colors, emojis and readable output. Help tables use
`tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

There is no docker-compose here: the filesystem is the source, so the dev loop is
`make fixture` once to generate something to serve, then `make run`.

The four probe tasks are the point of this file. `make playlist` is V3 made
visible, `make range` is V4 made visible, and `make validate` is V2's Proof line
turned into one command — each turns a criterion in SPEC.md into something you
can watch happen rather than something you infer from a passing test.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import os
import sys
import tempfile
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
    register_smoke_healthz,
)

FIXTURE_ASSET = "bbb"
"""Asset name `make fixture` generates. Matches the SPEC's worked example."""

FIXTURE_RENDITIONS = (
    # (rendition id, width, height, video bitrate)
    ("720p", 1280, 720, "2500k"),
    ("1080p", 1920, 1080, "5000k"),
)
"""Two rungs, because one rendition cannot demonstrate a ladder."""

FIXTURE_SECONDS = 30
FIXTURE_FPS = 30
FIXTURE_GOP = 60
"""A keyframe every 60 frames = every 2 s, forced and identical across both
renditions. That alignment is not cosmetic: V4's ABR criterion requires the two
renditions' segment boundaries to land on the same timestamps, and boundaries
come from keyframes. Generate them with different GOPs and no amount of correct
segmenting code will produce switchable renditions."""

runner = make_runner(
    crate="vod-streaming",
    help_title="🎬 vod-streaming",
    project_dir=PROJECT_DIR,
    help_footers=[
        ("Typical first run", "make setup && make sync && make fixture && make run"),
        ("See the packager work", "make assets → make playlist → make range"),
        ("Prove a segment decodes", "make validate   (needs ffprobe)"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
run_server = register_python_run(runner)
register_smoke_healthz(runner)
register_dev_stack(runner, use_cargo_watch=False)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


def _server_url(path: str = "") -> str:
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    return f"http://localhost:{port}{path}"


def _media_dir() -> Path:
    raw = runner.load_dotenv().get("MEDIA_DIR", "./media")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (runner.project_dir / path).resolve()


def _get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], bytes]:
    """GET `url`, returning (status, headers, body). Raises on transport failure."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _require_server() -> None:
    """Fail with a useful hint when the server isn't up yet."""
    try:
        _get(_server_url("/healthz"), timeout=2.0)
    except OSError:
        runner.fail(f"nothing listening on {_server_url()} — start it with `make run`")
        sys.exit(1)


def _asset() -> str:
    """Which asset the probes target. `A=name make playlist` to override."""
    return os.environ.get("A", FIXTURE_ASSET)


def _rendition() -> str:
    """Which rung the probes target. `R=1080p make range` to override."""
    return os.environ.get("R", FIXTURE_RENDITIONS[0][0])


def _todo_hint() -> None:
    print(
        f"   {C.DIM}if that raised: the server log names the vertical it needs — "
        f"that message is the worklist{C.RESET}"
    )


# --------------------------------------------------------------------------- #
# Media — something to serve
# --------------------------------------------------------------------------- #


@runner.task("fixture", "🎥", "Media", "Generate a two-rendition test asset with ffmpeg")
def fixture() -> None:
    """Synthesize `MEDIA_DIR/bbb/{720p,1080p}.mp4` so there is something to package.

    Deliberately generated rather than downloaded: it keeps no video in the repo,
    it is reproducible, and — most importantly — it pins the keyframe cadence, so
    the two renditions are actually switchable. See `FIXTURE_GOP`.
    """
    runner.require("ffmpeg", "Install ffmpeg (it is also what `make validate` uses).")
    target = _media_dir() / FIXTURE_ASSET
    target.mkdir(parents=True, exist_ok=True)

    for name, width, height, bitrate in FIXTURE_RENDITIONS:
        out = target / f"{name}.mp4"
        source = f"testsrc2=size={width}x{height}:rate={FIXTURE_FPS}:duration={FIXTURE_SECONDS}"
        runner.step("🎥", f"encoding {out.relative_to(_media_dir().parent)} …")
        runner.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", source,
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", bitrate,
                # Forced, identical GOPs. `-sc_threshold 0` stops the encoder
                # inserting extra keyframes at scene cuts, which would put the
                # two renditions' boundaries in different places.
                "-g", str(FIXTURE_GOP), "-keyint_min", str(FIXTURE_GOP), "-sc_threshold", "0",
                str(out),
            ]
        )  # fmt: skip
    runner.ok(f"{len(FIXTURE_RENDITIONS)} renditions under {target}")
    print(f"   {C.DIM}now: `make run`, then `make assets`{C.RESET}")


# --------------------------------------------------------------------------- #
# Probes — the SPEC's criteria, made visible
# --------------------------------------------------------------------------- #


@runner.task("assets", "📚", "Probe", "GET /assets — the scanned library")
def assets() -> None:
    _require_server()
    _, _, body = _get(_server_url("/assets"))
    print(body.decode("utf-8", "replace").strip())
    print()
    print(f"   {C.DIM}empty? check MEDIA_DIR in .env, or run `make fixture`{C.RESET}")


@runner.task("playlist", "📄", "Probe", "Print the master + media playlist (V3)")
def playlist() -> None:
    """V3 made visible: the two documents a player reads before any media."""
    _require_server()
    asset, rendition = _asset(), _rendition()
    for label, path in (
        ("master", f"/vod/{asset}/master.m3u8"),
        (f"media ({rendition})", f"/vod/{asset}/{rendition}/index.m3u8"),
    ):
        runner.step("📄", f"{label} — GET {path}")
        try:
            status, headers, body = _get(_server_url(path))
        except OSError as exc:
            runner.fail(f"request failed ({exc})")
            _todo_hint()
            sys.exit(1)
        print(f"   {C.DIM}HTTP {status} · {headers.get('Content-Type', '?')}{C.RESET}")
        print(body.decode("utf-8", "replace").strip() or f"   {C.DIM}(empty){C.RESET}")
        print()
    print(
        f"{C.DIM}V3 wants: EXT-X-VERSION:7, an EXT-X-MAP pointing at init.mp4, one "
        f"EXTINF per segment, and EXT-X-ENDLIST.{C.RESET}"
    )


@runner.task("range", "🎯", "Probe", "Send four Range requests at seg/0 and show the answers (V4)")
def range_probe() -> None:
    """V4 made visible: the four shapes `resolve_range` has to get right.

    Prints what came back rather than asserting, because the interesting part is
    the `Content-Range` and the body length agreeing with each other — which is
    exactly the pair a wrong implementation gets subtly out of step.
    """
    _require_server()
    asset, rendition = _asset(), _rendition()
    url = _server_url(f"/vod/{asset}/{rendition}/seg/0")

    try:
        status, headers, whole = _get(url)
    except OSError as exc:
        runner.fail(f"could not fetch the segment ({exc})")
        _todo_hint()
        sys.exit(1)
    if status != 200:
        runner.fail(f"GET {url} → HTTP {status}")
        _todo_hint()
        sys.exit(1)

    total = len(whole)
    runner.ok(f"segment 0 is {total} bytes · Accept-Ranges: {headers.get('Accept-Ranges', '—')}")
    print()

    probes = [
        ("bytes=0-99", "first 100 bytes → 206"),
        (f"bytes={max(total - 50, 0)}-", "open-ended to EOF → 206"),
        ("bytes=-100", "the LAST 100 bytes → 206 (not the first 100)"),
        (f"bytes={total + 1000}-", "past EOF → 416 with Content-Range: bytes */len"),
    ]
    for header, expectation in probes:
        try:
            status, headers, body = _get(url, headers={"Range": header})
        except OSError as exc:
            print(f"  {C.RED}{header:<22}{C.RESET}  transport error: {exc}")
            continue
        content_range = headers.get("Content-Range", "—")
        length = headers.get("Content-Length", "—")
        color = C.GREEN if status in (206, 416) else C.YELLOW
        print(f"  {C.BOLD}{header:<22}{C.RESET} {color}HTTP {status}{C.RESET}")
        print(f"      {C.DIM}Content-Range: {content_range} · Content-Length: {length}{C.RESET}")
        print(f"      {C.DIM}body: {len(body)} bytes · want: {expectation}{C.RESET}")
    print()


@runner.task("validate", "🔬", "Probe", "init.mp4 + seg/0 through ffprobe — V2's Proof")
def validate() -> None:
    """Concatenate the init segment with one media segment and ask a real decoder.

    This is the SPEC's `init_plus_segment_is_decodable` criterion as one command.
    ffprobe is not being polite here: if it reports a stream, a duration and a
    codec, the fragment is genuinely playable; if the `trun` data offset is wrong
    it will say so or report nonsense, which is the whole reason this check exists
    rather than a box-structure assertion you wrote yourself.
    """
    _require_server()
    runner.require("ffprobe", "Install ffmpeg (ffprobe ships with it).")
    asset, rendition = _asset(), _rendition()

    parts: list[bytes] = []
    for path in (f"/vod/{asset}/{rendition}/init.mp4", f"/vod/{asset}/{rendition}/seg/0"):
        runner.step("🔬", f"GET {path}")
        try:
            status, _, body = _get(_server_url(path))
        except OSError as exc:
            runner.fail(f"request failed ({exc})")
            _todo_hint()
            sys.exit(1)
        if status != 200:
            runner.fail(f"HTTP {status} for {path}")
            _todo_hint()
            sys.exit(1)
        parts.append(body)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
        handle.write(b"".join(parts))
        fragment = Path(handle.name)
    try:
        runner.step("🔬", f"ffprobe on init+seg ({fragment.stat().st_size} bytes)")
        code = runner.run(
            ["ffprobe", "-hide_banner", "-show_format", "-show_streams", str(fragment)],
            check=False,
        )
        print()
        if code == 0:
            runner.ok("ffprobe accepted the fragment — V2's decodability criterion holds")
        else:
            runner.fail("ffprobe rejected it — check the trun data_offset and the tfdt first")
            sys.exit(1)
    finally:
        fragment.unlink(missing_ok=True)


@runner.task("metrics", "📈", "Probe", "GET /metrics — the Prometheus scrape")
def metrics() -> None:
    _require_server()
    _, _, raw = _get(_server_url("/metrics"))
    lines = [ln for ln in raw.decode("utf-8", "replace").splitlines() if ln and ln[0] != "#"]
    if not lines:
        runner.warn("registry is empty — no counters recorded yet (observability horizontal)")
        return
    for line in lines:
        print(f"  {line}")
    print()
    runner.ok(f"{len(lines)} metric series")


@runner.task("profile", "🔥", "Bench", "Sample the running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    Muxing is the one genuinely CPU-bound thing in this project, so "where did the
    seconds go" is a graded question. py-spy attaches to a *running* process by
    PID, so start the server with `make run`, drive segment fetches at it in
    another shell, and sample while that is happening — a flamegraph of an idle
    event loop tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    pid = os.environ.get("PID")
    if not pid:
        runner.fail("set PID=<server pid> — py-spy samples a running process")
        print(
            f"   {C.DIM}e.g. `make run` in one shell, then "
            f"`PID=$(pgrep -f vod-streaming) make profile`{C.RESET}"
        )
        sys.exit(1)
    runner.step("🔥", "sampling for 10s — pull some segments meanwhile")
    runner.uv("run", "py-spy", "record", "--duration", "10", "--pid", pid, "--output", str(out))
    runner.ok(f"wrote {out}")


@runner.task("docker", "🐳", "Run", "Build and run the container (uvloop + PID-1 signals)")
def docker() -> None:
    """The check `make verify` cannot do.

    Under pytest the loop is the stdlib one and the process is not PID 1. In the
    container it is uvloop and it is PID 1, which is the only way to find out
    whether a `docker stop` really drains an in-flight segment read or truncates
    it. Mounts MEDIA_DIR read-only so the container serves the same library you
    have been testing against.
    """
    runner.require("docker", "Install Docker to build the image.")
    media = _media_dir()
    media.mkdir(parents=True, exist_ok=True)
    port = runner.load_dotenv().get("PORT", runner.config.default_port)

    runner.step("🐳", "building the image (from the repo root, for the uv workspace)…")
    runner.run(
        [
            "docker",
            "build",
            "-f",
            str(runner.project_dir / "Dockerfile"),
            "-t",
            "vod-streaming",
            ".",
        ],
        cwd=runner.workspace,
    )
    runner.step("🐳", f"running on :{port} — Ctrl-C, then watch for a clean shutdown")
    runner.run(
        [
            "docker", "run", "--rm", "-it",
            "-p", f"{port}:8080",
            "-v", f"{media}:/media:ro",
            "vod-streaming",
        ],
        check=False,
    )  # fmt: skip


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
