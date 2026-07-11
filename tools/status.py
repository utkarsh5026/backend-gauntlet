#!/usr/bin/env python3
"""backend-gauntlet — cross-project progress tracker.

A dependency-free dashboard that answers "where am I across every project?"
without a tracker you have to hand-maintain. It never drifts because it reads
the two sources of truth that ARE the work:

  * vertical challenges  ← `todo!()` markers in each vertical's `src/*.rs`
                            (a vertical is "done" once its module has no todo!())
  * horizontal checklist ← `- [ ]` / `- [x]` checkboxes in that project's SPEC.md

The only hand-written input is an optional, render-invisible status block at the
top of each SPEC.md, for the one thing code can't tell you — whether a project
is active/paused/blocked and why:

    <!-- status:
    state: active            # active | paused | blocked | done | not-started
    blocked-on: ~            # free text, or ~ for none
    -->

Usage:
    python3 tools/status.py            # one-line-per-project dashboard
    python3 tools/status.py 02         # drill into one project (verticals + open items)
    python3 tools/status.py trophies   # 🏆 the trophy case (achievements, auto-derived)
    make status                        # via the root Makefile wrapper
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"

# Order projects depend on each other in, so the dashboard can show the chain.
# (01's POST /api/links calls 02; both feed the later rungs.) Purely cosmetic.


# Color depth, best→worst: truecolor (24-bit) if the terminal advertises it,
# else 256-color (modern xterm/tmux), else legacy 16-color. NO_COLOR / non-tty
# silences everything. Palette is Catppuccin-Mocha-ish so it reads well on dark.
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_TRUE = os.environ.get("COLORTERM", "") in ("truecolor", "24bit")
_256 = _TRUE or "256color" in os.environ.get("TERM", "")


def _fg(rgb: tuple[int, int, int], idx: int, base: str) -> str:
    if not _COLOR:
        return ""
    if _TRUE:
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if _256:
        return f"\033[38;5;{idx}m"
    return f"\033[{base}m"


class C:
    RESET = "\033[0m" if _COLOR else ""
    BOLD = "\033[1m" if _COLOR else ""
    #                  truecolor          256  16   ← graceful degradation
    DIM = _fg((127, 132, 156), 245, "2")  # muted grey  (overlay1)
    RED = _fg((243, 139, 168), 210, "31")  # red
    GREEN = _fg((166, 227, 161), 114, "32")  # green
    YELLOW = _fg((249, 226, 175), 222, "33")  # yellow
    ORANGE = _fg((250, 179, 135), 216, "33")  # peach
    BLUE = _fg((137, 180, 250), 111, "34")  # blue
    MAGENTA = _fg((203, 166, 247), 183, "35")  # mauve
    CYAN = _fg((137, 220, 235), 117, "36")  # sky


STATE_STYLE = {
    "active": (C.GREEN, "active"),
    "blocked": (C.RED, "blocked"),
    "paused": (C.ORANGE, "paused"),
    "done": (C.BLUE, "done"),
    "not-started": (C.DIM, "not-started"),
}


VERTICAL_RE = re.compile(r"^### (V\d+)\.\s*(.+?)\s*$", re.MULTILINE)
SRC_RE = re.compile(r"src/([\w/]+\.rs)")
FRONTMATTER_RE = re.compile(r"<!--\s*status:(.*?)-->", re.DOTALL)
EMPHASIS_RE = re.compile(r"[*_`]")
TODO_RE = re.compile(r"\btodo!\s*\(")
CHECK_DONE_RE = re.compile(r"-\s*\[x\]", re.IGNORECASE)
CHECK_OPEN_RE = re.compile(r"-\s*\[ \]")


class Vertical:
    def __init__(
        self,
        vid: str,
        title: str,
        module: str | None,
        src_dir: Path,
        body: str = "",
        span: tuple[int, int] = (0, 0),
    ):
        self.vid = vid
        # Keep only the part before the "— *concept*" dash for a tidy title.
        self.title = EMPHASIS_RE.sub("", title.split("—")[0]).strip()
        self.module = module
        self.path = (src_dir / module) if module else None
        # The vertical's own "Done when ALL true" acceptance criteria. `span` is the
        # vertical's [start, end) offset in SPEC.md, so the horizontal checklist can
        # exclude these boxes and count them per-vertical instead.
        self.body = body
        self.span = span

    @property
    def checks(self) -> tuple[int, int]:
        """(done, total) acceptance-criteria checkboxes in this vertical's section."""
        done = len(CHECK_DONE_RE.findall(self.body))
        return done, done + len(CHECK_OPEN_RE.findall(self.body))

    @property
    def todos(self) -> int | None:
        """How many todo!() remain in this vertical's module (None = no module/file)."""
        if self.path is None or not self.path.exists():
            return None
        return len(TODO_RE.findall(self.path.read_text()))

    @property
    def state(self) -> str:
        """done | pending | unknown — derived purely from the code."""
        t = self.todos
        if t is None:
            return "unknown"
        return "done" if t == 0 else "pending"


class Project:
    def __init__(self, path: Path):
        self.path = path
        self.slug = path.name  # e.g. "01-url-shortener"
        self.num = path.name.split("-", 1)[0]
        self.name = path.name.split("-", 1)[1] if "-" in path.name else path.name
        spec = path / "SPEC.md"
        self.text = spec.read_text() if spec.exists() else ""
        self.front = self._parse_frontmatter()
        self.verticals = self._parse_verticals()

    def _parse_frontmatter(self) -> dict[str, str]:
        m = FRONTMATTER_RE.search(self.text)
        front: dict[str, str] = {}
        if not m:
            return front
        for line in m.group(1).splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" in line:
                k, _, v = line.partition(":")
                front[k.strip()] = v.strip()
        return front

    def _parse_verticals(self) -> list[Vertical]:
        src = self.path / "src"
        matches = list(VERTICAL_RE.finditer(self.text))
        out: list[Vertical] = []
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(self.text)
            # Stop the section at the horizontal checklist if it comes first.
            horiz = self.text.find("\n## ", start)
            if horiz != -1:
                end = min(end, horiz)
            body = self.text[start:end]
            src_m = SRC_RE.search(body)
            out.append(
                Vertical(
                    m.group(1),
                    m.group(2),
                    src_m.group(1) if src_m else None,
                    src,
                    body=body,
                    span=(start, end),
                )
            )
        return out

    # -- horizontal checklist ------------------------------------------------
    @property
    def checks(self) -> tuple[int, int]:
        """Horizontal-checklist boxes only. Per-vertical 'Done when' criteria are
        counted on their Vertical, so we subtract them from the file-wide total —
        which is a no-op for old-style SPECs (no checkboxes inside verticals)."""
        done = len(CHECK_DONE_RE.findall(self.text))
        total = done + len(CHECK_OPEN_RE.findall(self.text))
        for v in self.verticals:
            vdone, vtot = v.checks
            done -= vdone
            total -= vtot
        return done, total

    @property
    def open_items(self) -> list[str]:
        spans = [v.span for v in self.verticals]
        out = []
        for m in re.finditer(r"-\s*\[ \]\s*(.+)", self.text):
            if not any(s <= m.start() < e for s, e in spans):
                out.append(m.group(1).strip())
        return out

    # -- rollups -------------------------------------------------------------
    @property
    def v_done(self) -> int:
        return sum(1 for v in self.verticals if v.state == "done")

    @property
    def current(self) -> Vertical | None:
        """The first not-yet-done vertical — what to work on next."""
        for v in self.verticals:
            if v.state != "done":
                return v
        return None

    @property
    def state(self) -> str:
        st = self.front.get("state")
        if st in STATE_STYLE:
            return st
        # Infer when no frontmatter: all done -> done, none started -> not-started.
        cdone, ctot = self.checks
        if self.v_done == len(self.verticals) and cdone == ctot and ctot:
            return "done"
        if self.v_done == 0 and cdone == 0:
            return "not-started"
        return "active"

    @property
    def percent(self) -> int:
        cdone, ctot = self.checks
        vdone, vtot = self.v_done, len(self.verticals)
        num, den = vdone + cdone, vtot + ctot
        return round(100 * num / den) if den else 0


# --------------------------------------------------------------------------- #
# Trophies — achievements derived from the same sources of truth (code, SPECs,
# git history). Nothing to hand-maintain: they unlock themselves.
# --------------------------------------------------------------------------- #


def _git_history() -> list[tuple[dt.date, list[str]]]:
    """(author-date, touched paths) per commit, newest first. [] if git is absent."""
    try:
        out = subprocess.run(
            ["git", "log", "--pretty=format:%x1e%as", "--name-only"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return []
    commits: list[tuple[dt.date, list[str]]] = []
    for chunk in out.split("\x1e"):
        lines = [ln.strip() for ln in chunk.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        try:
            day = dt.date.fromisoformat(lines[0])
        except ValueError:
            continue
        commits.append((day, lines[1:]))
    return commits


def _streak(days: set[dt.date]) -> int:
    """Consecutive commit days ending today (or yesterday, so a streak survives
    checking the dashboard before today's first commit)."""
    d = dt.date.today()
    if d not in days:
        d -= dt.timedelta(days=1)
    n = 0
    while d in days:
        n += 1
        d -= dt.timedelta(days=1)
    return n


def trophies(projects: list[Project]) -> list[tuple[str, str, str, bool]]:
    """(icon, name, how-to-unlock, unlocked) — locked ones double as the quest log."""
    vdone = sum(p.v_done for p in projects)
    boxes = sum(p.checks[0] for p in projects) + sum(
        v.checks[0] for p in projects for v in p.verticals
    )
    active = sum(1 for p in projects if p.state == "active")
    slain = sum(1 for p in projects if p.state == "done")
    bench = any(
        (p.path / "bench").is_dir() and any((p.path / "bench").iterdir())
        for p in projects
    )
    design = bool(list((ROOT / "docs").glob("*design*.md"))) or any(
        list((p.path / "docs").glob("*design*.md"))
        for p in projects
        if (p.path / "docs").is_dir()
    )

    hist = _git_history()
    streak = _streak({d for d, _ in hist})
    per_project: dict[str, list[dt.date]] = {}
    for day, paths in hist:
        for path in paths:
            if path.startswith("projects/") and path.count("/") >= 2:
                per_project.setdefault(path.split("/")[1], []).append(day)
    necro = False
    for ds in per_project.values():
        s = sorted(set(ds))
        if any((b - a).days >= 30 for a, b in zip(s, s[1:])):
            necro = True
            break

    return [
        ("🩸", "First Blood", "complete your first vertical", vdone >= 1),
        ("⚔️", "Slayer", "5 verticals down across the gauntlet", vdone >= 5),
        ("🗡️", "Warlord", "15 verticals down", vdone >= 15),
        ("☑️", "Box Ticker", "10 acceptance boxes checked (with Proof)", boxes >= 10),
        ("🧾", "The Auditor", "50 acceptance boxes checked", boxes >= 50),
        ("🏎️", "Speed Demon", "a bench/ with real numbers exists", bench),
        ("📐", "The Architect", "a design doc written and committed", design),
        ("🐉", "Boss Slayer", "a whole project done — its boss defeated", slain >= 1),
        ("🐲", "Dragonrider", "three bosses down", slain >= 3),
        ("🐙", "Plate Spinner", "3+ projects active at once", active >= 3),
        ("🔥", "On Fire", "3-day commit streak", streak >= 3),
        ("🌋", "Unstoppable", "7-day commit streak", streak >= 7),
        ("🧟", "Necromancer", "revive a project untouched for 30+ days", necro),
    ]


def trophy_case(projects: list[Project]) -> None:
    tr = trophies(projects)
    won = sum(1 for t in tr if t[3])
    print()
    print(
        f"  {C.BOLD}{C.MAGENTA}🏆 trophy case{C.RESET}"
        f"  {C.DIM}·{C.RESET}  {C.BOLD}{won}/{len(tr)}{C.RESET} {C.DIM}unlocked"
        f" · earned by the code, the SPECs, and the git log — not by asking{C.RESET}"
    )
    print(rule())
    print()
    for icon, name, desc, ok in tr:
        if ok:
            print(
                f"    {icon}  {C.BOLD}{C.GREEN}{name:<14}{C.RESET} {desc}"
                f"  {C.GREEN}✓{C.RESET}"
            )
        else:
            print(f"    {C.DIM}🔒  {name:<14} {desc}{C.RESET}")
    print()
    nxt = next((t for t in tr if not t[3]), None)
    if nxt:
        print(f"  {C.YELLOW}→ nearest quest: {nxt[0]} {nxt[1]} — {nxt[2]}{C.RESET}\n")
    else:
        print(f"  {C.GREEN}the case is full. touch grass, champion. 🌱{C.RESET}\n")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


# Eighth-block partials let a bar resolve to 1/8 of a cell, so short bars
# (e.g. 12%) still show motion instead of snapping to whole blocks.
PARTIALS = " ▏▎▍▌▋▊▉"
RULE = 58
BADGE_W = len("[not-started]")  # widest badge, so bars line up across rows


def _bar_color(pct: int) -> str:
    if pct >= 80:
        return C.GREEN
    if pct >= 50:
        return C.YELLOW
    if pct >= 20:
        return C.ORANGE
    return C.RED


def bar(pct: int, width: int = 20) -> str:
    pct = max(0, min(100, pct))
    full, rem = divmod(round(width * 8 * pct / 100), 8)
    head = "█" * full + (PARTIALS[rem] if rem else "")
    tail = "░" * (width - full - (1 if rem else 0))
    return f"{_bar_color(pct)}{head}{C.DIM}{tail}{C.RESET}"


def rule(width: int = RULE) -> str:
    return f"  {C.DIM}{'─' * width}{C.RESET}"


def state_badge(state: str, pad: int = 0) -> str:
    color, label = STATE_STYLE.get(state, (C.DIM, state))
    badge = f"[{label}]"
    return f"{color}{badge}{C.RESET}{' ' * max(0, pad - len(badge))}"


def v_glyph(v: Vertical, is_current: bool) -> str:
    if v.state == "done":
        return f"{C.GREEN}{v.vid} ✅{C.RESET}"
    if v.state == "unknown":
        return f"{C.DIM}{v.vid} ❔{C.RESET}"
    if is_current:
        return f"{C.YELLOW}{v.vid} 🚧{C.RESET}"
    return f"{C.DIM}{v.vid} ⬜{C.RESET}"


def discover() -> list[Project]:
    if not PROJECTS.exists():
        return []
    dirs = sorted(
        d for d in PROJECTS.iterdir() if d.is_dir() and (d / "SPEC.md").exists()
    )
    return [Project(d) for d in dirs]


def dashboard(projects: list[Project]) -> None:
    # Aggregate across every project so the top line answers "overall, where am I?"
    vdone = sum(p.v_done for p in projects)
    vtot = sum(len(p.verticals) for p in projects)
    cdone = sum(p.checks[0] for p in projects)
    ctot = sum(p.checks[1] for p in projects)
    overall = round(100 * (vdone + cdone) / (vtot + ctot)) if (vtot + ctot) else 0

    print()
    print(
        f"  {C.BOLD}{C.MAGENTA}🦀 backend-gauntlet{C.RESET}"
        f"  {C.DIM}·  progress across all projects{C.RESET}\n"
    )
    print(
        f"  {C.BOLD}overall{C.RESET}  {bar(overall)} {C.BOLD}{overall:>3}%{C.RESET}"
        f"    {C.DIM}verticals{C.RESET} {C.BOLD}{vdone}/{vtot}{C.RESET}"
        f"  {C.DIM}·  checklist{C.RESET} {C.BOLD}{cdone}/{ctot}{C.RESET}"
    )
    tr = trophies(projects)
    won = [t for t in tr if t[3]]
    icons = " ".join(t[0] for t in won) if won else f"{C.DIM}(none yet){C.RESET}"
    print(
        f"  {C.BOLD}🏆 case{C.RESET}   {icons}"
        f"  {C.DIM}{len(won)}/{len(tr)} · python3 tools/status.py trophies{C.RESET}"
    )
    print(rule())
    print()

    for p in projects:
        pcdone, pctot = p.checks
        cur = p.current
        print(
            f"  {C.BOLD}{p.num}{C.RESET}  {C.CYAN}{C.BOLD}{p.name:<16}{C.RESET} "
            f"{state_badge(p.state, BADGE_W)}  {bar(p.percent)} {C.BOLD}{p.percent:>3}%{C.RESET}"
        )
        glyphs = "  ".join(v_glyph(v, v is cur) for v in p.verticals)
        print(
            f"     {C.DIM}verticals{C.RESET}  {glyphs}  {C.DIM}({p.v_done}/{len(p.verticals)}){C.RESET}"
        )
        print(
            f"     {C.DIM}checklist{C.RESET}  {bar(round(100*pcdone/pctot) if pctot else 0, 10)} "
            f"{C.DIM}{pcdone}/{pctot}{C.RESET}"
        )
        if cur is not None:
            todos = cur.todos
            tail = f" {C.DIM}· {cur.module} ({todos} todo!()){C.RESET}" if todos else ""
            print(f"     {C.YELLOW}→ next  {cur.vid} {cur.title}{C.RESET}{tail}")
        blocked = p.front.get("blocked-on", "~")
        if blocked and blocked != "~":
            print(f"     {C.RED}⛔ blocked-on: {blocked}{C.RESET}")
        print()

    print(rule())
    print(
        f"  {C.GREEN}✅ done{C.RESET}   {C.YELLOW}🚧 current{C.RESET}   "
        f"{C.DIM}⬜ pending{C.RESET}   {C.DIM}❔ unknown{C.RESET}"
    )
    print(
        f"  {C.DIM}drill in with{C.RESET} {C.CYAN}python3 tools/status.py <NN>{C.RESET}"
        f"  {C.DIM}· lives in code + SPEC.md, no manual upkeep{C.RESET}\n"
    )


def detail(p: Project) -> None:
    print()
    print(
        f"  {C.BOLD}{C.MAGENTA}{p.num} — {p.name}{C.RESET}  {state_badge(p.state)}  "
        f"{bar(p.percent)} {C.BOLD}{p.percent}%{C.RESET}"
    )
    print(rule())
    print()
    print(
        f"  {C.BOLD}{C.YELLOW}Verticals{C.RESET} "
        f"{C.DIM}from-scratch primitives · ({p.v_done}/{len(p.verticals)}){C.RESET}"
    )
    cur = p.current
    for v in p.verticals:
        glyph = v_glyph(v, v is cur)
        todos = v.todos
        if v.module is None:
            note = f"{C.DIM}(no module declared in SPEC){C.RESET}"
        elif todos is None:
            note = f"{C.RED}{v.module} — file missing{C.RESET}"
        elif todos == 0:
            note = f"{C.GREEN}{v.module} — complete{C.RESET}"
        else:
            note = f"{C.DIM}{v.module} — {todos} todo!() left{C.RESET}"
        cd, ct = v.checks
        crit = (
            f"  {C.DIM}[{cd}/{ct}]{C.RESET} {bar(round(100 * cd / ct) if ct else 0, 10)}"
            if ct
            else ""
        )
        print(f"    {glyph}  {v.title:<28} {note}{crit}")
    print()
    cdone, ctot = p.checks
    print(
        f"  {C.BOLD}{C.YELLOW}Horizontal checklist{C.RESET}  "
        f"{bar(round(100*cdone/ctot) if ctot else 0, 12)} {C.DIM}{cdone}/{ctot} done{C.RESET}"
    )
    for item in p.open_items:
        print(f"    {C.DIM}⬜ {EMPHASIS_RE.sub('', item)[:80]}{C.RESET}")
    if not p.open_items:
        print(f"    {C.GREEN}all checklist items done 🎉{C.RESET}")
    print()


def main(argv: list[str]) -> int:
    projects = discover()
    if not projects:
        print(f"{C.RED}no projects with a SPEC.md found under {PROJECTS}{C.RESET}")
        return 1
    if argv and argv[0] in ("trophies", "trophy", "case", "🏆"):
        trophy_case(projects)
        return 0
    if argv:
        key = argv[0].lstrip("0") or "0"
        match = next(
            (p for p in projects if p.num.lstrip("0") == key or argv[0] in p.slug), None
        )
        if match is None:
            print(f"{C.RED}no project matching '{argv[0]}'{C.RESET}")
            return 1
        detail(match)
    else:
        dashboard(projects)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
