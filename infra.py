#!/usr/bin/env python3
"""backend-gauntlet — infra control panel.

A dependency-free local web dashboard that answers "which project's Docker
deps are up right now, what's colliding, and let me start/stop them" — the
runtime companion to `status.py` (which tracks *code* progress).

It reads two sources of truth and never drifts:

  * DECLARED  ← each `projects/*/docker-compose.yml` (services, images, host ports)
  * ACTUAL    ← one `docker ps -a`, mapped back to a project via the
                `com.docker.compose.project` label Compose stamps on containers.

Because several projects bind the same host ports (01/04/12 → 5432,
01/02/03 → 6379, 05 → 9000), you *cannot* run everything at once. So the panel's
real job is showing who owns a port and refusing to start a project whose port
is already held by someone else.

Usage:
    python3 infra.py                 # serve on http://127.0.0.1:7878
    python3 infra.py --port 9999
    python3 infra.py --no-open       # don't auto-open a browser
    make infra                       # via the root Makefile wrapper

Stdlib only. Shells out to `docker` / `docker compose`; never uses shell=True.
Binds to loopback only — it can start and stop containers, so it is not for
exposing on a network.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECTS = ROOT / "projects"

# Per-project action lock — never run two `up`/`down` on the same stack at once.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(slug: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(slug, threading.Lock())


# --------------------------------------------------------------------------- #
# DECLARED — parse the compose files. A focused parser for the subset of YAML
# these files use (services → image/build/ports), robust to the anchors and
# depends_on/environment blocks that appear in 07 and 10. No PyYAML dependency.
# --------------------------------------------------------------------------- #


def parse_compose(path: Path) -> list[dict]:
    """Return [{name, image, build, ports:[{host,container}]}] for one file."""
    services: dict[str, dict] = {}
    in_services = False
    cur: str | None = None
    section: str | None = None  # the current indent-4 key inside a service

    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if indent == 0:
            # Top-level key. `services:` opens the block; anything else (incl.
            # `x-*:` anchor blocks) closes it so their children are ignored.
            in_services = stripped.rstrip().startswith("services:")
            cur = None
            section = None
            continue
        if not in_services:
            continue

        if indent == 2 and stripped.endswith(":"):
            cur = stripped[:-1].strip()
            services[cur] = {"name": cur, "image": None, "build": False, "ports": []}
            section = None
            continue
        if cur is None:
            continue

        if indent == 4:
            if stripped.endswith(":"):
                section = stripped[:-1].strip()
                if section == "build":
                    services[cur]["build"] = True
            else:
                section = None
                if stripped.startswith("image:"):
                    services[cur]["image"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("build:"):
                    services[cur]["build"] = True
            continue

        # indent >= 6 — list/dict items belonging to `section`.
        if section == "ports" and stripped.startswith("-"):
            item = stripped[1:].strip()
            item = item.split("#", 1)[0].strip().strip("\"'")
            if not item:
                continue
            # forms: "host:container", "host:container/proto", "ip:host:container",
            # or a bare container port (not published → no host binding).
            parts = item.split(":")
            if len(parts) >= 2:
                host = parts[-2].strip()
                container = parts[-1].split("/")[0].strip()
                if host.isdigit():
                    services[cur]["ports"].append(
                        {"host": int(host), "container": int(container) if container.isdigit() else container}
                    )

    return list(services.values())


def discover_projects() -> list[dict]:
    """Every projects/NN-name with a docker-compose.yml, sorted by NN."""
    out = []
    for d in sorted(PROJECTS.iterdir()):
        if not d.is_dir():
            continue
        compose = d / "docker-compose.yml"
        if not compose.exists():
            compose = d / "compose.yml"
        num, _, name = d.name.partition("-")
        out.append(
            {
                "num": num,
                "slug": d.name,
                "name": name or d.name,
                "compose": str(compose) if compose.exists() else None,
                "services": parse_compose(compose) if compose.exists() else [],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# ACTUAL — one `docker ps -a`, keyed by the compose-project label.
# --------------------------------------------------------------------------- #

_FMT = (
    '{{.Label "com.docker.compose.project"}}\t'
    '{{.Label "com.docker.compose.service"}}\t'
    "{{.State}}\t{{.Status}}\t{{.Ports}}\t{{.Names}}"
)


def _health(status: str) -> str | None:
    s = status.lower()
    if "(healthy)" in s:
        return "healthy"
    if "(unhealthy)" in s:
        return "unhealthy"
    if "health: starting" in s:
        return "starting"
    return None


def _host_ports(ports: str) -> list[int]:
    """Pull published host ports out of a docker `Ports` string."""
    found: set[int] = set()
    for chunk in ports.split(","):
        chunk = chunk.strip()
        if "->" not in chunk:
            continue
        left = chunk.split("->", 1)[0]  # e.g. 0.0.0.0:5432 or [::]:9000-9001
        port = left.rsplit(":", 1)[-1]
        if port.isdigit():
            found.add(int(port))
        elif "-" in port:  # a published range, e.g. 9000-9001
            lo, _, hi = port.partition("-")
            if lo.isdigit() and hi.isdigit():
                found.update(range(int(lo), int(hi) + 1))
    return sorted(found)


def docker_ps() -> tuple[list[dict], bool]:
    """Live containers + whether the docker daemon answered."""
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc", "--format", _FMT],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return [], False
    except Exception:
        return [], False
    if r.returncode != 0:
        return [], False

    rows = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        while len(parts) < 6:
            parts.append("")
        project, service, state, status, ports, names = parts[:6]
        rows.append(
            {
                "project": project,
                "service": service,
                "state": state,
                "status": status,
                "health": _health(status),
                "host_ports": _host_ports(ports),
                "names": names,
            }
        )
    return rows, True


# --------------------------------------------------------------------------- #
# Merge declared + actual into the JSON the UI renders.
# --------------------------------------------------------------------------- #


def build_status() -> dict:
    projects = discover_projects()
    containers, docker_ok = docker_ps()

    # live map: host port -> the compose project (or external name) holding it.
    port_owner: dict[int, str] = {}
    for c in containers:
        if c["state"] != "running":
            continue
        owner = c["project"] or c["names"]
        for p in c["host_ports"]:
            port_owner.setdefault(p, owner)

    # declared contention: host port -> which project slugs declare it.
    declared_by: dict[int, list[str]] = {}
    for proj in projects:
        for svc in proj["services"]:
            for pt in svc["ports"]:
                declared_by.setdefault(pt["host"], []).append(proj["slug"])

    result = []
    for proj in projects:
        by_service = {
            c["service"]: c for c in containers if c["project"] == proj["slug"]
        }
        services = []
        up = 0
        for svc in proj["services"]:
            live = by_service.get(svc["name"])
            state = live["state"] if live else "absent"
            if state == "running":
                up += 1
            services.append(
                {
                    "name": svc["name"],
                    "image": svc["image"] or ("(built locally)" if svc["build"] else "?"),
                    "ports": svc["ports"],
                    "state": state,
                    "status": live["status"] if live else "",
                    "health": live["health"] if live else None,
                }
            )

        # who else wants my ports (static), and which of my ports are held by
        # a *different* project right now (live) — the thing that blocks `up`.
        contested = []
        blocked_ports = []
        for svc in proj["services"]:
            for pt in svc["ports"]:
                host = pt["host"]
                others = [s for s in declared_by.get(host, []) if s != proj["slug"]]
                if others:
                    contested.append({"port": host, "others": sorted(set(others))})
                owner = port_owner.get(host)
                if owner and owner != proj["slug"]:
                    blocked_ports.append({"port": host, "held_by": owner})

        total = len(proj["services"])
        if total == 0:
            overall = "none"
        elif up == total:
            overall = "up"
        elif up == 0:
            overall = "down"
        else:
            overall = "partial"

        result.append(
            {
                **{k: proj[k] for k in ("num", "slug", "name", "compose")},
                "services": services,
                "up": up,
                "total": total,
                "overall": overall,
                "contested": _dedup(contested),
                "blocked_ports": _dedup(blocked_ports),
            }
        )

    return {"docker_ok": docker_ok, "projects": result}


def _dedup(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = json.dumps(it, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


# --------------------------------------------------------------------------- #
# CONTROL — up / down / restart / swap, streamed line-by-line to the browser.
# `--ansi never --progress plain` makes compose emit clean event lines
# ("Container X Started") instead of spinner control codes.
# --------------------------------------------------------------------------- #

_COMPOSE = ["docker", "compose", "--ansi", "never", "--progress", "plain"]


def _project_by_num(num: str) -> dict | None:
    for p in discover_projects():
        if p["num"] == num or p["slug"] == num:
            return p
    return None


def _run_stream(cmd: list[str], timeout: float = 300):
    """Yield the command's output lines as they arrive; return its exit code.
    Kills the process if the generator is closed early (client disconnected)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    start = time.monotonic()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = _tidy(line.rstrip())
            if line:
                yield line
            if time.monotonic() - start > timeout:
                proc.kill()
                yield f"✖ timed out (>{timeout:.0f}s), killed"
                break
        return proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()


_LOGFMT = re.compile(r'^time="[^"]*"\s+level=(\w+)\s+msg="(.*)"\s*$')


def _tidy(line: str) -> str:
    """Clean compose's logfmt noise: drop the boring warning, humanize the rest."""
    m = _LOGFMT.match(line)
    if not m:
        return line
    if m.group(2) == "No services to build":
        return ""
    return f"{m.group(1)}: {m.group(2)}"


def stream_action(num: str, action: str):
    """Generator of human-readable log lines for one up/down/restart/swap."""
    proj = _project_by_num(num)
    if not proj or not proj["compose"]:
        yield f"✖ no compose for project {num!r}"
        return
    if action not in ("up", "down", "restart", "swap"):
        yield f"✖ unknown action {action!r}"
        return

    lock = _lock_for(proj["slug"])
    if not lock.acquire(blocking=False):
        yield "✖ another action is already running for this project"
        return
    t0 = time.monotonic()
    try:
        if action == "swap":
            # Stop whoever holds our ports (repo stacks via compose stop,
            # external containers via docker stop), then fall through to `up`.
            blockers = _blockers(proj)
            if not blockers:
                yield "· nothing to evict — ports are free"
            for owner, names in blockers.items():
                other = _project_by_num(owner)
                yield f"⏹ stopping {owner} …"
                if other and other["compose"]:
                    yield from _run_stream(
                        _COMPOSE + ["-f", other["compose"], "-p", owner, "stop"],
                        timeout=120,
                    )
                else:
                    # `docker stop` just echoes each name back — dress it up.
                    for line in _run_stream(["docker", "stop", *names], timeout=120):
                        yield f"· stopped {line}"
            action = "up"

        if action in ("up", "restart"):
            blocker = _collision_guard(proj)
            if blocker:
                yield "✖ " + blocker
                return

        base = _COMPOSE + ["-f", proj["compose"], "-p", proj["slug"]]
        needs_build = any(s["build"] for s in proj["services"])
        if action == "up":
            cmd = base + ["up", "-d"] + (["--build"] if needs_build else [])
        elif action == "restart":
            cmd = base + ["restart"]
        else:
            cmd = base + ["down"]

        yield f"▶ {action} {proj['slug']}"
        rc = yield from _run_stream(cmd)
        dt = time.monotonic() - t0
        yield (f"✔ done in {dt:.1f}s" if rc == 0 else f"✖ exited {rc} after {dt:.1f}s")
    finally:
        lock.release()


def _blockers(proj: dict) -> dict[str, list[str]]:
    """owner → running container names that hold a host port this project needs."""
    containers, ok = docker_ps()
    if not ok:
        return {}
    needed = {pt["host"] for svc in proj["services"] for pt in svc["ports"]}
    out: dict[str, list[str]] = {}
    for c in containers:
        if c["state"] != "running":
            continue
        who = c["project"] or c["names"]
        if who == proj["slug"]:
            continue
        if needed & set(c["host_ports"]):
            out.setdefault(who, []).append(c["names"])
    return out


def _collision_guard(proj: dict) -> str | None:
    """Refuse to start if a needed host port is held by another container."""
    containers, ok = docker_ps()
    if not ok:
        return None
    owner: dict[int, str] = {}
    for c in containers:
        if c["state"] == "running":
            who = c["project"] or c["names"]
            for p in c["host_ports"]:
                owner.setdefault(p, who)
    for svc in proj["services"]:
        for pt in svc["ports"]:
            held = owner.get(pt["host"])
            if held and held != proj["slug"]:
                return (
                    f"port {pt['host']} is already held by '{held}'. "
                    f"Stop it first, or this project's {svc['name']} can't bind."
                )
    return None


# --------------------------------------------------------------------------- #
# HTTP server.
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/status"):
            self._json(build_status())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/action":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "bad json"}, 400)
            return

        # Stream log lines as they happen. HTTP/1.0 close-delimits the body, so
        # no Content-Length is needed and fetch() reads it progressively.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        gen = stream_action(str(body.get("nn", "")), str(body.get("action", "")))
        try:
            for line in gen:
                self.wfile.write((line + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser went away — gen.close() below kills the subprocess
        finally:
            gen.close()


def main():
    args = sys.argv[1:]
    port = 7878
    auto_open = True
    if "--no-open" in args:
        auto_open = False
    if "--port" in args:
        port = int(args[args.index("--port") + 1])

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"infra control panel → {url}  (Ctrl-C to stop)")
    if auto_open:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.shutdown()


# --------------------------------------------------------------------------- #
# The page. Inline HTML/CSS/JS so it works fully offline. Catppuccin-Mocha-ish
# to match status.py's terminal palette.
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>backend-gauntlet · infra</title>
<style>
  :root{
    --bg:#181825; --panel:#1e1e2e; --line:#313244; --line2:#45475a;
    --text:#cdd6f4; --sub:#9399b2; --dim:#6c7086;
    --green:#a6e3a1; --red:#f38ba8; --yellow:#f9e2af; --peach:#fab387;
    --blue:#89b4fa; --sky:#89dceb;
    --sans:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 var(--sans)}
  code,.mono{font-family:var(--mono)}

  header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;
    padding:18px 26px;border-bottom:1px solid var(--line)}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.2px}
  header h1 .g{color:var(--dim);font-weight:400}
  .summary{display:flex;gap:18px;align-items:baseline;margin-left:auto;
    font-size:13px;color:var(--sub)}
  .summary b{color:var(--text);font-weight:600}
  .summary .warnc{color:var(--peach)}
  .summary .off{color:var(--red)}
  #clock{color:var(--dim);font-size:12px;font-family:var(--mono)}

  .grid{display:grid;gap:16px;padding:26px;
    grid-template-columns:repeat(auto-fill,minmax(320px,1fr));max-width:1400px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:16px 18px;display:flex;flex-direction:column;gap:12px;
    transition:border-color .15s}
  .card:hover{border-color:var(--line2)}

  .chead{display:flex;align-items:center;gap:10px}
  .dot{width:9px;height:9px;border-radius:50%;flex:none}
  .dot.up{background:var(--green)} .dot.partial{background:var(--yellow)}
  .dot.down{background:var(--dim)}
  .ctitle{font-weight:600;font-size:15px}
  .cnum{color:var(--dim);font-size:12px;font-family:var(--mono)}
  .cstate{margin-left:auto;font-size:12px;color:var(--sub);font-family:var(--mono)}

  .svcs{display:flex;flex-direction:column}
  .svc{display:grid;grid-template-columns:auto 1fr auto auto;align-items:center;
    gap:10px;padding:7px 0;border-top:1px solid var(--line)}
  .svc:first-child{border-top:none;padding-top:2px}
  .sdot{width:7px;height:7px;border-radius:50%}
  .sdot.running{background:var(--green)} .sdot.absent{background:var(--line2)}
  .sdot.exited,.sdot.dead{background:var(--red)}
  .sdot.restarting,.sdot.created,.sdot.paused{background:var(--yellow)}
  .sname{font-weight:500;cursor:default}
  .sports{font-family:var(--mono);font-size:12px;color:var(--sky)}
  .hbadge{width:6px;height:6px;border-radius:50%;background:transparent}
  .hbadge.healthy{background:var(--green)} .hbadge.unhealthy{background:var(--red)}
  .hbadge.starting{background:var(--yellow)}

  .note{font-size:12px;color:var(--dim);display:flex;align-items:center;gap:6px}
  .note.block{color:var(--red)}

  .foot{display:flex;align-items:center;gap:8px;margin-top:2px}
  button{font:inherit;font-size:12.5px;cursor:pointer;border:1px solid var(--line2);
    background:transparent;color:var(--sub);padding:5px 13px;border-radius:8px;
    transition:all .12s}
  button:hover:not(:disabled){color:var(--text);border-color:var(--sub)}
  button:disabled{opacity:.35;cursor:not-allowed}
  button.primary{color:var(--green);border-color:rgba(166,227,161,.4)}
  button.primary:hover:not(:disabled){background:rgba(166,227,161,.1)}
  button.danger{color:var(--red);border-color:rgba(243,139,168,.35)}
  button.danger:hover:not(:disabled){background:rgba(243,139,168,.1)}
  .working{color:var(--peach);font-size:12px;font-family:var(--mono);
    display:inline-flex;align-items:center;gap:6px}
  .spin{width:10px;height:10px;border:2px solid var(--line2);border-top-color:var(--peach);
    border-radius:50%;animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .log{font-size:11px;line-height:1.65;color:var(--sub);background:var(--bg);
    border:1px solid var(--line);border-radius:8px;padding:9px 11px;max-height:180px;
    overflow:auto;font-family:var(--mono);margin-top:2px}
  .log>div{white-space:pre-wrap}
  .log .ok{color:var(--green)} .log .err{color:var(--red)}
  .log .off{color:var(--dim)} .log .hdr{color:var(--blue)}

  .empty{padding:2px 26px 30px;color:var(--dim);font-size:13px}
  .empty .mono{color:var(--sub)}
</style>
</head>
<body>
<header>
  <h1>backend-gauntlet <span class="g">/ infra</span></h1>
  <div class="summary" id="summary"></div>
  <span id="clock"></span>
</header>
<div id="grid" class="grid"></div>
<div class="empty" id="empty"></div>

<script>
const grid=document.getElementById('grid');
const busy=new Set();     // slugs with an in-flight action
const logs={};            // slug -> last action output

function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e;}

async function refresh(){
  let data;
  try{ data=await (await fetch('/api/status')).json(); }catch(e){ return; }
  document.getElementById('clock').textContent=new Date().toLocaleTimeString();
  render(data);
}

function render(data){
  const withInfra=data.projects.filter(p=>p.total>0);
  const bare=data.projects.filter(p=>p.total===0);

  const upStacks=withInfra.filter(p=>p.overall==='up').length;
  const blocked=withInfra.reduce((n,p)=>n+(p.blocked_ports||[]).length,0);
  const sum=document.getElementById('summary');
  sum.innerHTML='';
  if(!data.docker_ok){ sum.append(el('span','off','● docker unreachable')); }
  else{
    sum.append(html(`<span><b>${upStacks}</b>/${withInfra.length} stacks up</span>`));
    if(blocked) sum.append(html(`<span class="warnc">⚠ ${blocked} port conflict${blocked>1?'s':''}</span>`));
  }

  grid.innerHTML='';
  for(const p of withInfra) grid.append(cardFor(p));

  const empty=document.getElementById('empty');
  empty.innerHTML='';
  if(bare.length){
    empty.append(document.createTextNode('no infra yet: '));
    empty.append(el('span','mono', bare.map(p=>p.num).join('  ')));
  }
}

function html(s){const t=document.createElement('template');t.innerHTML=s.trim();return t.content.firstChild;}

function cardFor(p){
  const card=el('div','card');

  const head=el('div','chead');
  head.append(el('span','dot '+p.overall));
  head.append(el('span','ctitle',p.name));
  head.append(el('span','cnum',p.num));
  head.append(el('span','cstate',`${p.up}/${p.total}`));
  card.append(head);

  const svcs=el('div','svcs');
  for(const s of p.services){
    const row=el('div','svc');
    row.append(el('span','sdot '+s.state));
    const name=el('span','sname',s.name); name.title=s.image;
    row.append(name);
    row.append(el('span','sports', s.ports.length?':'+s.ports.map(x=>x.host).join(','):''));
    const h=el('span','hbadge'+(s.health?' '+s.health:'')); if(s.health)h.title=s.health;
    row.append(h);
    svcs.append(row);
  }
  card.append(svcs);

  for(const b of (p.blocked_ports||[]))
    card.append(html(`<div class="note block">⛔ port ${b.port} held by <span class="mono">${b.held_by}</span></div>`));
  const contested=(p.contested||[]).map(c=>c.port);
  if(contested.length){
    const others=[...new Set((p.contested||[]).flatMap(c=>c.others.map(o=>o.split('-')[0])))];
    card.append(html(`<div class="note">↔ shares :${[...new Set(contested)].join(', :')} with ${others.join(', ')}</div>`));
  }

  const foot=el('div','foot');
  if(busy.has(p.slug)){
    const w=el('span','working'); w.append(el('span','spin')); w.append(document.createTextNode('working…'));
    foot.append(w);
  }else{
    const mk=(label,act,cls,confirmMsg)=>{const b=el('button',cls,label);
      b.onclick=()=>{ if(confirmMsg && !confirm(confirmMsg)) return; act_on(p.num,p.slug,act); };
      return b;};
    const holders=[...new Set((p.blocked_ports||[]).map(b=>b.held_by))];
    if(p.up<p.total){
      if(holders.length){
        const b=mk('⇄ Swap in','swap','primary',
          `Stop ${holders.join(', ')} and start ${p.name}?`);
        b.title=`stops ${holders.join(', ')}, then starts this stack`;
        foot.append(b);
      }else foot.append(mk('Start','up','primary'));
    }
    if(p.up>0){ foot.append(mk('Stop','down','danger')); foot.append(mk('Restart','restart','')); }
  }
  card.append(foot);

  if(logs[p.slug]!=null){
    const box=el('div','log'); box.id='log-'+p.slug;
    for(const l of logs[p.slug].split('\n')) if(l) box.append(logLine(l));
    card.append(box);
    requestAnimationFrame(()=>{box.scrollTop=box.scrollHeight;});
  }
  return card;
}

// Colorize one log line by what compose is telling us.
function lineClass(l){
  if(/^✖|error|fail|denied|timed out|no such/i.test(l)) return 'err';
  if(/^✔|Started|Healthy$|Running/.test(l)) return 'ok';
  if(/Stopp|Removed|Removing|Killed|^⏹/.test(l)) return 'off';
  if(/^▶|^\$|^·/.test(l)) return 'hdr';
  return '';
}
function logLine(l){ return el('div',lineClass(l),l); }

async function act_on(nn,slug,action){
  busy.add(slug); logs[slug]=''; await refresh();   // card now shows spinner + empty log box
  const box=()=>document.getElementById('log-'+slug);
  const push=(l)=>{ if(!l) return; logs[slug]+=l+'\n';
    const b=box(); if(b){ b.append(logLine(l)); b.scrollTop=b.scrollHeight; } };
  try{
    const r=await fetch('/api/action',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({nn,action})});
    const reader=r.body.getReader(); const dec=new TextDecoder(); let buf='';
    for(;;){
      const {done,value}=await reader.read();
      if(done) break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n'); buf=lines.pop();   // keep the partial tail
      lines.forEach(push);
    }
    if(buf) push(buf);
  }catch(e){ push('✖ '+e); }
  busy.delete(slug);
  await refresh();
}

refresh();
setInterval(()=>{ if(busy.size===0) refresh(); }, 2500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
