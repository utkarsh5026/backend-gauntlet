#!/usr/bin/env python3
"""backend-gauntlet — docs reader.

A dependency-free local web reader for every markdown file in the repo, so you
can keep the docs open in one browser tab and code in the other window instead
of juggling editor splits.

It is the browser sibling of `md.py` (which renders one file in glow) and the
reading companion to `infra.py` (containers) and `status.py` (progress):

  * DISCOVERS  ← every `*.md` under the repo, grouped by project, plus the
                 per-project `docs/`, SPEC/CONCEPTS/RESEARCH and root docs.
  * RENDERS    ← markdown → HTML in-process (no pip deps, no CDN, no network),
                 including GFM tables, task lists, fenced code with syntax
                 highlighting, and relative links between docs.
  * FOLLOWS    ← links to source files (`[router.rs](../src/router.rs)`) open
                 the real file, syntax-highlighted, in the same reader.
  * RELOADS    ← polls mtimes; a doc you edit re-renders in place, scroll kept.

Usage:
    python3 tools/docs.py              # serve on http://127.0.0.1:7979
    python3 tools/docs.py --port 9000  # pick a different port
    python3 tools/docs.py --open       # also launch a browser tab
    python3 tools/docs.py 10           # open straight to project 10's SPEC
    make docs                          # via the root Makefile wrapper

Prints the URL on startup. Stdlib only. Binds to loopback only — it serves
file contents from the repo, so it is not for exposing on a network.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"

# Directories that never hold docs worth reading (and would swamp the tree).
SKIP_DIRS = {
    "node_modules",
    "target",
    ".git",
    "dist",
    ".sqlx",
    "__pycache__",
    ".venv",
    "venv",
    ".vite",
    ".pytest_cache",
    ".ruff_cache",
}

# Per-project reading order: the ticket first, then the theory, then the docs.
KIND_ORDER = {"SPEC": 0, "CONCEPTS": 1, "RESEARCH": 2, "README": 3, "doc": 4, "other": 5}

# Images are served as bytes; every other non-.md file a doc links to (.rs,
# .toml, .json, …) is followed and syntax-highlighted in the reader itself.
RAW_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
MIME = {
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".ico": "image/x-icon",
}

STATUS_RE = re.compile(r"<!--\s*status:(.*?)-->", re.DOTALL)


# --------------------------------------------------------------------------- #
# Discovery — walk the repo once, group by project, keep it cheap enough to
# redo on every /api/tree call so new docs appear without a restart.
# --------------------------------------------------------------------------- #


@dataclass
class Doc:
    rel: str  # repo-relative posix path
    title: str
    label: str  # what the sidebar shows
    kind: str  # SPEC | CONCEPTS | RESEARCH | README | doc | other
    group: str  # group id it belongs to


@dataclass
class Group:
    gid: str
    title: str
    num: str = ""
    state: str = ""
    docs: list[Doc] = field(default_factory=list)


def _walk_md() -> list[Path]:
    found: list[Path] = []
    stack = [ROOT]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if e.name not in SKIP_DIRS:
                    stack.append(e)
            elif e.suffix.lower() == ".md":
                found.append(e)
    return found


def _first_heading(path: Path) -> str:
    """The doc's own `# Title`, falling back to a prettified filename."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            in_fence = False
            for _ in range(60):
                line = fh.readline()
                if not line:
                    break
                s = line.strip()
                if s.startswith("```"):
                    in_fence = not in_fence
                    continue
                if not in_fence and s.startswith("# "):
                    return _plain(s[2:].strip())
    except OSError:
        pass
    return path.stem.replace("-", " ").replace("_", " ")


def _plain(text: str) -> str:
    """Strip inline markdown so headings read cleanly in menus and the TOC."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_~]{1,3}", "", text)
    return text.strip()


def _kind_of(path: Path) -> str:
    stem = path.stem.upper()
    if stem in ("SPEC", "CONCEPTS", "RESEARCH", "README"):
        return stem
    if path.parent.name == "docs":
        return "doc"
    return "other"


def _project_state(pdir: Path) -> str:
    """Reuse status.py's render-invisible SPEC status block for the sidebar dot."""
    spec = pdir / "SPEC.md"
    if not spec.exists():
        return ""
    m = STATUS_RE.search(spec.read_text(encoding="utf-8", errors="replace")[:4000])
    if not m:
        return ""
    for line in m.group(1).splitlines():
        if line.strip().startswith("state:"):
            return line.split(":", 1)[1].split("#")[0].strip()
    return ""


def _doc_label(path: Path, kind: str, title: str) -> str:
    if kind in ("SPEC", "CONCEPTS", "RESEARCH", "README"):
        return kind
    if kind == "doc":
        # `04-how-snapshots-work.md` → "04 · how snapshots work"
        stem = path.stem
        m = re.match(r"^(\d+)[-_](.*)$", stem)
        if m:
            return f"{m.group(1)} · {m.group(2).replace('-', ' ')}"
        return stem.replace("-", " ")
    return title or path.name


def build_tree() -> list[Group]:
    groups: dict[str, Group] = {}

    def group_for(rel: str) -> Group:
        parts = rel.split("/")
        if len(parts) == 1:
            gid, title, num = "root", "repo", ""
        elif parts[0] == "projects" and len(parts) > 2:
            slug = parts[1]
            num = slug.split("-")[0]
            gid, title = f"p{slug}", slug.split("-", 1)[-1].replace("-", " ")
        elif parts[0] == "crates":
            gid, title, num = "crates", "crates", ""
        elif parts[0] == ".claude":
            gid, title, num = "claude", "claude commands", ""
        else:
            gid, title, num = parts[0], parts[0], ""
        if gid not in groups:
            g = Group(gid=gid, title=title, num=num)
            if gid.startswith("p") and len(parts) > 1:
                g.state = _project_state(ROOT / "projects" / parts[1])
            groups[gid] = g
        return groups[gid]

    for path in _walk_md():
        rel = path.relative_to(ROOT).as_posix()
        kind = _kind_of(path)
        title = _first_heading(path)
        g = group_for(rel)
        g.docs.append(
            Doc(rel=rel, title=title, label=_doc_label(path, kind, title), kind=kind, group=g.gid)
        )

    for g in groups.values():
        g.docs.sort(key=lambda d: (KIND_ORDER.get(d.kind, 9), d.rel))

    def gkey(g: Group) -> tuple:
        if g.gid == "root":
            return (0, "")
        if g.num:
            return (1, g.num)
        return (2, g.title)

    return sorted(groups.values(), key=gkey)


# --------------------------------------------------------------------------- #
# Syntax highlighting — a deliberately small tokenizer. Good enough to make
# Rust/JSON/bash readable at a glance; not a parser, and never pretends to be.
# --------------------------------------------------------------------------- #


@dataclass
class LangSpec:
    keywords: set[str]
    types: set[str] = field(default_factory=set)
    line_comment: str = "#"
    block_comment: bool = False
    consts: set[str] = field(default_factory=set)


def _kw(words: str) -> set[str]:
    """A keyword set from a whitespace-separated blob — kept readable in source."""
    return set(words.split())


RUST_KW = _kw("""as async await break const continue crate dyn else enum extern false fn for
if impl in let loop match mod move mut pub ref return self Self static struct super trait true
type unsafe use where while union macro_rules""")
RUST_TY = _kw("""u8 u16 u32 u64 u128 usize i8 i16 i32 i64 i128 isize f32 f64 bool char str String
Vec Option Result Box Arc Rc RefCell Mutex RwLock HashMap HashSet BTreeMap VecDeque Duration
Instant Path PathBuf Cow""")
PY_KW = _kw("""and as assert async await break class continue def del elif else except finally
for from global if import in is lambda nonlocal not or pass raise return try while with yield
True False None self""")
SH_KW = _kw("""if then elif else fi for while do done case esac function return exit export local
set unset echo cd source read shift trap eval exec printf""")
SQL_KW = _kw("""select from where insert into values update set delete create table drop alter
index unique primary key foreign references join left right inner outer on group by order having
limit offset returning with as and or not null default constraint begin commit rollback
distinct count sum avg min max case when then else end exists in between like asc desc""")
TS_KW = _kw("""abstract any as async await boolean break case catch class const continue declare
default delete do else enum export extends false finally for from function get if implements
import in instanceof interface let new null number of private protected public readonly return
set static string super switch this throw true try type typeof undefined var void while yield""")
GO_KW = _kw("""break case chan const continue default defer else fallthrough for func go goto if
import interface map package range return select struct switch type var true false nil""")

LANG_SPECS: dict[str, LangSpec] = {
    "rust": LangSpec(RUST_KW, RUST_TY, "//", True),
    "python": LangSpec(PY_KW, set(), "#", False),
    "bash": LangSpec(SH_KW, set(), "#", False),
    "sql": LangSpec(SQL_KW, set(), "--", True),
    "typescript": LangSpec(TS_KW, set(), "//", True),
    "javascript": LangSpec(TS_KW, set(), "//", True),
    "go": LangSpec(GO_KW, set(), "//", True),
    "json": LangSpec(set(), set(), "", False, {"true", "false", "null"}),
    "toml": LangSpec(set(), set(), "#", False, {"true", "false"}),
    "yaml": LangSpec(set(), set(), "#", False, {"true", "false", "null", "yes", "no"}),
    "makefile": LangSpec(set(), set(), "#", False),
    "dockerfile": LangSpec(
        set("FROM RUN CMD LABEL EXPOSE ENV ADD COPY ENTRYPOINT VOLUME USER WORKDIR ARG".split()),
        set(), "#", False,
    ),
    "text": LangSpec(set(), set(), "", False),
}
LANG_ALIAS = {
    "rs": "rust", "py": "python", "sh": "bash", "shell": "bash", "zsh": "bash",
    "console": "bash", "ts": "typescript", "tsx": "typescript", "js": "javascript",
    "jsx": "javascript", "yml": "yaml", "psql": "sql", "postgres": "sql", "golang": "go",
    "docker": "dockerfile", "make": "makefile", "": "text", "plain": "text", "txt": "text",
}


def _esc(s: str, quote: bool = False) -> str:
    return html_mod.escape(s, quote=quote)


def highlight(code: str, lang: str) -> str:
    """Escaped HTML for `code`, with <span class=…> tokens when we know the lang."""
    key = LANG_ALIAS.get(lang.lower().strip(), lang.lower().strip())
    spec = LANG_SPECS.get(key)
    if spec is None:
        return _esc(code)

    parts = []
    if spec.block_comment:
        parts.append(r"(?P<cmtb>/\*.*?\*/)" if key != "sql" else r"(?P<cmtb>/\*.*?\*/)")
    if spec.line_comment:
        parts.append(rf"(?P<cmt>{re.escape(spec.line_comment)}[^\n]*)")
    if key == "rust":
        parts.append(r"(?P<attr>\#!?\[[^\]\n]*\])")
        parts.append(r"(?P<life>&?'[a-z_][a-z_0-9]*\b)")
    parts.append(r'(?P<str>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\\n])*\')')
    parts.append(r"(?P<num>\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?[a-z0-9]*\b)")
    if key == "bash":
        parts.append(r"(?P<flag>(?<=\s)--?[A-Za-z][\w-]*)")
        parts.append(r"(?P<var>\$\{?\w+\}?)")
    parts.append(r"(?P<word>[A-Za-z_][A-Za-z_0-9]*!?)")
    pattern = re.compile("|".join(parts), re.DOTALL)

    out: list[str] = []
    pos = 0
    for m in pattern.finditer(code):
        out.append(_esc(code[pos : m.start()]))
        kind = m.lastgroup
        tok = m.group()
        if kind in ("cmt", "cmtb"):
            cls = "c-cmt"
        elif kind == "str":
            cls = "c-str"
        elif kind == "num":
            cls = "c-num"
        elif kind == "attr":
            cls = "c-attr"
        elif kind == "life":
            cls = "c-life"
        elif kind == "flag":
            cls = "c-flag"
        elif kind == "var":
            cls = "c-var"
        else:  # word
            low = tok.lower()
            after = code[m.end() : m.end() + 1]
            if tok in spec.keywords or (key in ("sql", "dockerfile") and low in spec.keywords):
                cls = "c-kw"
            elif low in spec.consts:
                cls = "c-num"
            elif tok in spec.types or (tok[:1].isupper() and key in ("rust", "typescript", "go")):
                cls = "c-ty"
            elif tok.endswith("!") or after == "(":
                cls = "c-fn"
            else:
                cls = ""
        out.append(f'<span class="{cls}">{_esc(tok)}</span>' if cls else _esc(tok))
        pos = m.end()
    out.append(_esc(code[pos:]))
    return "".join(out)


# --------------------------------------------------------------------------- #
# Markdown → HTML. A focused GFM subset covering exactly what this repo's docs
# use: headings, fences, tables, task lists (including the SPEC's [~]/[✔]),
# blockquotes, nested lists, and links that resolve between docs and sources.
# --------------------------------------------------------------------------- #

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*(\S.*)?$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HR_RE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")
BQ_RE = re.compile(r"^\s*>\s?(.*)$")
ULI_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
OLI_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
TASK_RE = re.compile(r"^\[([ xX~✔✓])\]\s*(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:-]*-{2,}[\s:|-]*\|?\s*$")
HTML_LINE_RE = re.compile(r"^\s*</?([A-Za-z][\w-]*)")
INLINE_HTML_OK = ("br", "kbd", "sub", "sup", "b", "i", "em", "strong", "small", "u", "mark")
BLOCK_HTML_OK = {
    "details", "summary", "div", "img", "picture", "source", "table", "thead", "tbody",
    "tr", "td", "th", "p", "a", "span", "center", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "hr", "br", "video", "figure", "figcaption", "pre",
}


class Markdown:
    """One instance per rendered document (it accumulates the TOC and slugs)."""

    def __init__(self, doc_rel: str):
        self.dir = PurePosixPath(doc_rel).parent
        self.toc: list[dict] = []
        self.slugs: dict[str, int] = {}
        self.mermaid = False
        self.title = ""
        self._stash: list[str] = []

    # -- links ------------------------------------------------------------- #

    def _slug(self, text: str) -> str:
        base = re.sub(r"[^\w\s-]", "", _plain(text).lower()).strip()
        base = re.sub(r"[\s_]+", "-", base) or "section"
        n = self.slugs.get(base, 0)
        self.slugs[base] = n + 1
        return base if n == 0 else f"{base}-{n}"

    def _resolve(self, href: str) -> tuple[str, str]:
        """(href, css-class) — repo-relative targets become in-app hash routes."""
        href = href.strip()
        if not href:
            return "#", "x"
        if re.match(r"^(https?:|mailto:|vscode:|data:|//)", href):
            return _esc(href, True), "ext"
        if href.startswith("#"):
            return _esc(href, True), "anchor"
        anchor = ""
        if "#" in href:
            href, anchor = href.split("#", 1)
            if not href:
                return _esc("#" + anchor, True), "anchor"
        target = (self.dir / href) if not href.startswith("/") else PurePosixPath(href.lstrip("/"))
        rel = PurePosixPath(*_normalize(target.parts)).as_posix()
        abs_path = ROOT / rel
        if not abs_path.exists():
            return _esc(f"#/{rel}", True), "miss"
        if abs_path.is_dir():
            return _esc(f"#/{rel}", True), "dir"
        suffix = abs_path.suffix.lower()
        if suffix in RAW_SUFFIXES:
            return _esc(f"/raw?p={rel}", True), "ext"
        frag = f"::{anchor}" if anchor else ""
        cls = "doc" if suffix == ".md" else "src"
        return _esc(f"#/{rel}{frag}", True), cls

    def _img_src(self, src: str) -> str:
        src = src.strip()
        if re.match(r"^(https?:|data:|//)", src):
            return _esc(src, True)
        target = (self.dir / src) if not src.startswith("/") else PurePosixPath(src.lstrip("/"))
        rel = PurePosixPath(*_normalize(target.parts)).as_posix()
        return _esc(f"/raw?p={rel}", True)

    # -- inline ------------------------------------------------------------ #

    def _keep(self, html: str) -> str:
        self._stash.append(html)
        return f"\x00{len(self._stash) - 1}\x00"

    def inline(self, text: str) -> str:
        # 1. code spans come out first so nothing rewrites their insides
        def code_span(m: re.Match) -> str:
            return self._keep(f"<code>{_esc(m.group(2).strip())}</code>")

        text = re.sub(r"(`+)([^`]+?)\1", code_span, text)
        text = _esc(text)
        # a tiny whitelist of inline HTML the docs actually use
        for tag in INLINE_HTML_OK:
            text = re.sub(rf"&lt;(/?{tag})\s*/?&gt;", r"<\1>", text, flags=re.I)

        def image(m: re.Match) -> str:
            alt, src = m.group(1), m.group(2)
            return self._keep(
                f'<img src="{self._img_src(src)}" alt="{_esc(alt, True)}" loading="lazy">'
            )

        text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)", image, text)

        def link(m: re.Match) -> str:
            label, href = m.group(1), m.group(2)
            url, cls = self._resolve(href)
            ext = ' target="_blank" rel="noreferrer"' if cls == "ext" else ""
            return self._keep(f'<a class="l-{cls}" href="{url}"{ext}>{self._emphasis(label)}</a>')

        text = re.sub(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]\(([^)\s]+)(?:\s+[^)]*)?\)", link, text)
        text = self._emphasis(text)

        def autolink(m: re.Match) -> str:
            u = m.group(0)
            return self._keep(
                f'<a class="l-ext" href="{_esc(u, True)}" '
                f'target="_blank" rel="noreferrer">{u}</a>'
            )

        text = re.sub(r"(?<![\"'=(>])\bhttps?://[^\s<>()\[\]]+", autolink, text)

        # restore stashes (links may contain stashed code spans → loop)
        for _ in range(6):
            if "\x00" not in text:
                break
            text = re.sub(r"\x00(\d+)\x00", lambda m: self._stash[int(m.group(1))], text)
        return text

    def _emphasis(self, text: str) -> str:
        text = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"<strong><em>\1</em></strong>", text)
        text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<![\w\\])__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<![\w*\\])\*(?=\S)([^*]+?)(?<=\S)\*(?!\w)", r"<em>\1</em>", text)
        text = re.sub(r"(?<![\w_\\])_(?=\S)([^_]+?)(?<=\S)_(?!\w)", r"<em>\1</em>", text)
        text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text)
        return text

    # -- blocks ------------------------------------------------------------ #

    def render(self, text: str) -> str:
        lines = text.replace("\r\n", "\n").replace("\t", "    ").split("\n")
        return self.blocks(lines)

    def blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue

            m = FENCE_RE.match(line)
            if m:
                i = self._fence(lines, i, m, out)
                continue

            if line.lstrip().startswith("<!--"):
                while i < n and "-->" not in lines[i]:
                    i += 1
                i += 1
                continue

            m = HEADING_RE.match(line)
            if m:
                level = len(m.group(1))
                raw = m.group(2)
                text_html = self.inline(raw)
                plain = _plain(raw)
                slug = self._slug(raw)
                if level == 1 and not self.title:
                    self.title = plain
                if 2 <= level <= 3:
                    self.toc.append({"level": level, "id": slug, "text": plain})
                out.append(
                    f'<h{level} id="{slug}">'
                    f'<a class="hlink" href="#{slug}">#</a>{text_html}</h{level}>'
                )
                i += 1
                continue

            if HR_RE.match(line):
                out.append("<hr>")
                i += 1
                continue

            if BQ_RE.match(line):
                i = self._blockquote(lines, i, out)
                continue

            if (
                "|" in line
                and i + 1 < n
                and "|" in lines[i + 1]
                and TABLE_SEP_RE.match(lines[i + 1])
            ):
                i = self._table(lines, i, out)
                continue

            if ULI_RE.match(line) or OLI_RE.match(line):
                i = self._list(lines, i, out)
                continue

            hm = HTML_LINE_RE.match(line)
            if hm and hm.group(1).lower() in BLOCK_HTML_OK:
                buf = []
                while i < n and lines[i].strip():
                    buf.append(lines[i])
                    i += 1
                out.append("\n".join(buf))
                continue

            before = i
            i = self._paragraph(lines, i, out)
            if i <= before:  # belt-and-braces: never spin on a line
                i = before + 1
        return "\n".join(out)

    def _fence(self, lines: list[str], i: int, m: re.Match, out: list[str]) -> int:
        indent, ticks = len(m.group(1)), m.group(2)[0]
        lang, label_text = _fence_info(m.group(3) or "")
        close = re.compile(rf"^\s*{re.escape(ticks)}{{{len(m.group(2))},}}\s*$")
        body: list[str] = []
        i += 1
        while i < len(lines) and not close.match(lines[i]):
            body.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i])
            i += 1
        i += 1
        code = "\n".join(body)
        if lang.lower() == "mermaid":
            self.mermaid = True
            out.append(f'<pre class="mermaid">{_esc(code)}</pre>')
            return i
        cls = "lang path" if ("/" in label_text or ":" in label_text) else "lang"
        label = f'<span class="{cls}">{_esc(label_text)}</span>' if label_text else ""
        out.append(
            f'<div class="cb">{label}<button class="copy" type="button">copy</button>'
            f'<pre><code class="lang-{_esc(lang.lower(), True) or "text"}">'
            f"{highlight(code, lang)}</code></pre></div>"
        )
        return i

    def _blockquote(self, lines: list[str], i: int, out: list[str]) -> int:
        inner: list[str] = []
        while i < len(lines):
            m = BQ_RE.match(lines[i])
            if m:
                inner.append(m.group(1))
                i += 1
            elif lines[i].strip() and not HEADING_RE.match(lines[i]) and inner:
                inner.append(lines[i].strip())  # lazy continuation
                i += 1
            else:
                break
        body = self.blocks(inner)
        cls = "note"
        head = inner[0] if inner else ""
        alert = re.match(r"^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", head, re.I)
        if alert:
            cls = f"note {alert.group(1).lower()}"
            body = self.blocks(inner[1:])
        out.append(f'<blockquote class="{cls}">{body}</blockquote>')
        return i

    def _table(self, lines: list[str], i: int, out: list[str]) -> int:
        header = _split_row(lines[i])
        aligns = []
        for cell in _split_row(lines[i + 1]):
            c = cell.strip()
            if c.startswith(":") and c.endswith(":"):
                aligns.append("center")
            elif c.endswith(":"):
                aligns.append("right")
            else:
                aligns.append("left")
        i += 2
        rows: list[list[str]] = []
        while i < len(lines) and lines[i].strip() and "|" in lines[i]:
            rows.append(_split_row(lines[i]))
            i += 1

        def cells(cs: list[str], tag: str) -> str:
            got = []
            for k, c in enumerate(cs):
                a = aligns[k] if k < len(aligns) else "left"
                got.append(f'<{tag} class="a-{a}">{self.inline(c.strip())}</{tag}>')
            return "".join(got)

        body = "".join(f"<tr>{cells(r, 'td')}</tr>" for r in rows)
        out.append(
            f'<div class="tw"><table><thead><tr>{cells(header, "th")}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>"
        )
        return i

    def _list(self, lines: list[str], i: int, out: list[str]) -> int:
        first = ULI_RE.match(lines[i]) or OLI_RE.match(lines[i])
        if first is None:  # unreachable — callers only enter on a list line
            return self._paragraph(lines, i, out)
        ordered = OLI_RE.match(lines[i]) is not None and ULI_RE.match(lines[i]) is None
        base = len(first.group(1))
        items: list[list[str]] = []
        n = len(lines)

        while i < n:
            line = lines[i]
            if not line.strip():
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and (_indent(lines[j]) > base or _is_item(lines[j], base)):
                    if items:
                        items[-1].append("")
                    i = j
                    continue
                break
            ind = _indent(line)
            m = ULI_RE.match(line) or OLI_RE.match(line)
            if m and ind <= base + 1:
                if ind < base:
                    break
                items.append([m.group(3)])
                i += 1
            elif ind > base and items:
                items[-1].append(line[min(base + 2, ind) :])
                i += 1
            else:
                break

        rendered = []
        has_task = False
        for content in items:
            li_class = ""
            tm = TASK_RE.match(content[0]) if content else None
            if tm:
                has_task = True
                mark = tm.group(1)
                state = (
                    "done" if mark in "xX✔✓" else ("open-field" if mark == "~" else "open")
                )
                box = {"done": "✔", "open-field": "~", "open": ""}[state]
                content = [tm.group(2)] + content[1:]
                li_class = f' class="task {state}"'
                prefix = f'<span class="box {state}">{box}</span>'
            else:
                prefix = ""
            # a "lead" of plain lines renders inline; the rest as nested blocks
            k = 0
            while k < len(content) and content[k].strip() and not (
                ULI_RE.match(content[k]) or OLI_RE.match(content[k]) or FENCE_RE.match(content[k])
            ):
                k += 1
            lead = self.inline(" ".join(x.strip() for x in content[:k])) if k else ""
            rest = self.blocks(content[k:]) if k < len(content) else ""
            rendered.append(f"<li{li_class}>{prefix}{lead}{rest}</li>")

        tag = "ol" if ordered else "ul"
        cls = ' class="tasks"' if has_task else ""
        out.append(f"<{tag}{cls}>{''.join(rendered)}</{tag}>")
        return i

    def _paragraph(self, lines: list[str], i: int, out: list[str]) -> int:
        # Always consumes its first line — `blocks()` relies on that to make
        # progress, and a paragraph is the branch of last resort.
        n = len(lines)
        buf: list[str] = [lines[i].strip()]
        i += 1
        while i < n:
            line = lines[i]
            if not line.strip():
                break
            if (
                HEADING_RE.match(line)
                or FENCE_RE.match(line)
                or HR_RE.match(line)
                or BQ_RE.match(line)
                or ULI_RE.match(line)
                or OLI_RE.match(line)
                or line.lstrip().startswith("<!--")
            ):
                break
            if "|" in line and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1]):
                break
            buf.append(line.strip())
            i += 1
        out.append(f"<p>{self.inline(' '.join(buf))}</p>")
        return i


def _fence_info(info: str) -> tuple[str, str]:
    """(highlight language, display label) for a fence's info string.

    Covers the three forms these docs use: a bare language (```rust), a
    language with attributes (```rust,no_run), and the file-reference form
    (```199:200:projects/06-object-store/src/routes.rs) — where the language
    has to come from the referenced file's extension.
    """
    info = info.strip()
    if not info:
        return "", ""
    head = re.split(r"[,\s]", info, maxsplit=1)[0]
    if ":" in head or "/" in head:
        suffix = PurePosixPath(head.split(":")[-1]).suffix.lstrip(".")
        return suffix, info
    return head, info


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_item(line: str, base: int) -> bool:
    m = ULI_RE.match(line) or OLI_RE.match(line)
    return bool(m) and _indent(line) >= base


def _normalize(parts: tuple[str, ...]) -> list[str]:
    stack: list[str] = []
    for p in parts:
        if p in ("", "."):
            continue
        if p == "..":
            if stack:
                stack.pop()
        else:
            stack.append(p)
    return stack


def _split_row(line: str) -> list[str]:
    """Split a table row on `|`, respecting code spans and escapes."""
    s = line.strip()
    cells: list[str] = []
    cur: list[str] = []
    in_code = False
    k = 0
    while k < len(s):
        c = s[k]
        if c == "\\" and k + 1 < len(s):
            cur.append(s[k + 1])
            k += 2
            continue
        if c == "`":
            in_code = not in_code
        if c == "|" and not in_code:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        k += 1
    cells.append("".join(cur))
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return cells


# --------------------------------------------------------------------------- #
# Documents — render markdown, or a source file as one highlighted block.
# --------------------------------------------------------------------------- #


def safe_path(rel: str) -> Path | None:
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return None
    try:
        p = (ROOT / rel).resolve()
        p.relative_to(ROOT)
    except (ValueError, OSError):
        return None
    if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts[:-1]):
        return None
    # Never serve real secrets, even on loopback — `.env.example` is fine.
    if p.name.startswith(".env") and p.name != ".env.example":
        return None
    return p if p.is_file() else None


def render_doc(rel: str) -> dict:
    path = safe_path(rel)
    if path is None:
        return {"error": f"not found: {rel}"}
    suffix = path.suffix.lower()
    stat = path.stat()
    if suffix == ".md":
        md = Markdown(rel)
        body = md.render(path.read_text(encoding="utf-8", errors="replace"))
        return {
            "path": rel,
            "abs": str(path),
            "title": md.title or _first_heading(path),
            "html": body,
            "toc": md.toc,
            "mermaid": md.mermaid,
            "mtime": stat.st_mtime,
            "kind": _kind_of(path),
            "source": False,
        }
    if stat.st_size > 2_000_000:
        return {"error": f"{rel} is too large to display ({stat.st_size // 1024} KB)"}
    text = path.read_text(encoding="utf-8", errors="replace")
    lang = LANG_ALIAS.get(suffix.lstrip("."), suffix.lstrip("."))
    return {
        "path": rel,
        "abs": str(path),
        "title": path.name,
        "html": f'<div class="cb src"><pre><code>{highlight(text, lang)}</code></pre></div>',
        "toc": [],
        "mermaid": False,
        "mtime": stat.st_mtime,
        "kind": "source",
        "source": True,
        "lines": text.count("\n") + 1,
    }


def search(query: str, limit: int = 60) -> list[dict]:
    """Case-insensitive full-text sweep. ~170 docs — brute force is instant."""
    q = query.strip().lower()
    if len(q) < 2:
        return []
    results: list[dict] = []
    for g in build_tree():
        for doc in g.docs:
            path = ROOT / doc.rel
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            low = text.lower()
            if q not in low:
                continue
            hits = 0
            for ln, line in enumerate(text.split("\n"), 1):
                if q in line.lower():
                    hits += 1
                    if hits <= 3:
                        snippet = line.strip()
                        if len(snippet) > 190:
                            at = snippet.lower().find(q)
                            snippet = "…" + snippet[max(0, at - 70) : at + 120] + "…"
                        results.append(
                            {
                                "path": doc.rel,
                                "title": doc.title,
                                "group": g.title,
                                "line": ln,
                                "snippet": snippet,
                                "count": 0,
                            }
                        )
            for r in results[-min(hits, 3) :]:
                r["count"] = hits
    results.sort(key=lambda r: (-r["count"], r["path"], r["line"]))
    return results[:limit]


def version_stamp() -> str:
    """Cheap fingerprint of every doc's mtime — drives the browser's live reload."""
    acc = 0.0
    count = 0
    for path in _walk_md():
        try:
            acc += path.stat().st_mtime
            count += 1
        except OSError:
            pass
    return f"{count}:{acc:.0f}"


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "gauntlet-docs"

    def log_message(self, format, *args):  # quiet — keep the terminal clean
        pass

    def _send(self, code: int, body: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, payload) -> None:
        self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        route = url.path

        if route in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return

        if route == "/api/tree":
            self._json(
                {
                    "groups": [
                        {
                            "id": g.gid,
                            "title": g.title,
                            "num": g.num,
                            "state": g.state,
                            "docs": [
                                {"path": d.rel, "title": d.title, "label": d.label, "kind": d.kind}
                                for d in g.docs
                            ],
                        }
                        for g in build_tree()
                    ],
                    "version": version_stamp(),
                }
            )
            return

        if route == "/api/doc":
            self._json(render_doc(qs.get("p", [""])[0]))
            return

        if route == "/api/search":
            self._json({"results": search(qs.get("q", [""])[0])})
            return

        if route == "/api/version":
            self._json({"version": version_stamp()})
            return

        if route == "/raw":
            path = safe_path(qs.get("p", [""])[0])
            if path is None:
                self._send(404, b"not found", "text/plain")
                return
            ctype = MIME.get(path.suffix.lower(), "application/octet-stream")
            self._send(200, path.read_bytes(), ctype, cache=True)
            return

        self._send(404, b"not found", "text/plain")


def _resolve_start(arg: str | None) -> str:
    """`docs.py 10` / `docs.py 10 CONCEPTS` → the doc to open on load."""
    if not arg:
        return ""
    if (ROOT / arg).is_file():
        return arg
    nn = f"{int(arg):02d}" if arg.isdigit() else arg
    hits = sorted(PROJECTS.glob(f"{nn}-*")) or sorted(PROJECTS.glob(f"*{arg}*"))
    if hits:
        return (hits[0] / "SPEC.md").relative_to(ROOT).as_posix()
    return ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Browse the repo's markdown docs in a browser tab.")
    ap.add_argument("target", nargs="?", help="project NN or a path to open on load")
    ap.add_argument("--port", type=int, default=7979)
    ap.add_argument("--open", action="store_true", help="launch a browser tab")
    args = ap.parse_args(argv)

    start = _resolve_start(args.target)
    url = f"http://127.0.0.1:{args.port}/"
    if start:
        url += f"#/{start}"

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"error: cannot bind 127.0.0.1:{args.port} — {e}", file=sys.stderr)
        print("hint: pass --port to pick another (make docs PORT=8080)", file=sys.stderr)
        return 1

    total = sum(len(g.docs) for g in build_tree())
    print(f"backend-gauntlet docs → {url}")
    print(
        f"  {total} markdown files · live-reloads on save · ctrl-K to search · ctrl-C to stop",
        flush=True,
    )
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.shutdown()
    return 0


# --------------------------------------------------------------------------- #
# The page. Inline HTML/CSS/JS so it works fully offline — same Catppuccin
# Mocha palette as infra.py and status.py's terminal output.
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>backend-gauntlet · docs</title>
<style>
  :root{
    --bg:#181825; --panel:#1e1e2e; --panel2:#11111b; --line:#313244; --line2:#45475a;
    --text:#cdd6f4; --sub:#9399b2; --dim:#6c7086;
    --green:#a6e3a1; --red:#f38ba8; --yellow:#f9e2af; --peach:#fab387;
    --blue:#89b4fa; --sky:#89dceb; --mauve:#cba6f7; --pink:#f5c2e7; --teal:#94e2d5;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    --mono:ui-monospace,SFMono-Regular,"JetBrainsMono Nerd Font",Menlo,Consolas,monospace;
    --sidebar:290px; --toc:210px;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--text);font:15px/1.7 var(--sans);
    -webkit-font-smoothing:antialiased}
  ::selection{background:rgba(137,180,250,.28)}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-thumb{background:var(--line);border-radius:6px}
  ::-webkit-scrollbar-thumb:hover{background:var(--line2)}
  ::-webkit-scrollbar-track{background:transparent}

  /* ── layout ───────────────────────────────────────────────────────────── */
  .app{display:grid;grid-template-columns:var(--sidebar) minmax(0,1fr);min-height:100vh}
  .app.nonav{grid-template-columns:minmax(0,1fr)}
  .app.nonav aside{display:none}

  aside{position:sticky;top:0;height:100vh;overflow:hidden;background:var(--panel2);
    border-right:1px solid var(--line);display:flex;flex-direction:column}
  .brand{padding:16px 18px 12px;display:flex;align-items:center;gap:9px;flex:none}
  .brand b{font-size:14px;font-weight:600;letter-spacing:.2px}
  .brand .g{color:var(--dim);font-weight:400}
  .brand .sp{margin-left:auto;font-size:11px;color:var(--dim);font-family:var(--mono)}
  .filter{padding:0 14px 12px;flex:none}
  .filter input{width:100%;background:var(--bg);border:1px solid var(--line);color:var(--text);
    border-radius:9px;padding:8px 11px;font:13px var(--sans);outline:none}
  .filter input:focus{border-color:var(--line2)}
  .filter input::placeholder{color:var(--dim)}
  nav{overflow-y:auto;padding:0 8px 40px;flex:1}

  .grp{margin-bottom:2px}
  .ghead{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:8px;
    cursor:pointer;user-select:none;font-size:12.5px;color:var(--sub);
    text-transform:lowercase;letter-spacing:.3px}
  .ghead:hover{background:rgba(255,255,255,.03);color:var(--text)}
  .ghead .chev{color:var(--dim);font-size:9px;width:8px;flex:none;transition:transform .15s}
  .ghead.open .chev{transform:rotate(90deg)}
  .ghead .num{font-family:var(--mono);color:var(--dim);font-size:11px}
  .ghead .st{width:6px;height:6px;border-radius:50%;margin-left:auto;flex:none;
    background:var(--line2)}
  .st.active{background:var(--green)} .st.done{background:var(--blue)}
  .st.paused{background:var(--yellow)} .st.blocked{background:var(--red)}
  .st.not-started{background:var(--line2)}
  .gitems{display:none;margin:2px 0 8px 9px;padding-left:9px;border-left:1px solid var(--line)}
  .grp.open .gitems{display:block}

  a.item{display:flex;gap:8px;align-items:baseline;padding:5px 9px;border-radius:7px;
    color:var(--sub);text-decoration:none;font-size:13px;line-height:1.45;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  a.item:hover{background:rgba(255,255,255,.04);color:var(--text)}
  a.item.on{background:rgba(137,180,250,.13);color:var(--blue)}
  a.item .k{font-family:var(--mono);font-size:10px;color:var(--dim);flex:none}
  a.item.on .k{color:var(--blue)}
  a.item.kSPEC .k{color:var(--peach)} a.item.kCONCEPTS .k{color:var(--mauve)}
  a.item.kRESEARCH .k{color:var(--teal)}

  /* ── main ─────────────────────────────────────────────────────────────── */
  main{min-width:0;display:flex;flex-direction:column}
  .bar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;
    padding:11px 26px;background:rgba(24,24,37,.86);backdrop-filter:blur(10px);
    border-bottom:1px solid var(--line);font-size:12.5px;min-height:46px}
  .crumb{font-family:var(--mono);color:var(--dim);white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis}
  .crumb b{color:var(--sub);font-weight:500}
  .bar .sp{margin-left:auto}
  .bar button,.bar a.btn{background:transparent;border:1px solid var(--line);color:var(--sub);
    border-radius:8px;padding:4px 11px;font:12px var(--sans);cursor:pointer;
    text-decoration:none;white-space:nowrap}
  .bar button:hover,.bar a.btn:hover{border-color:var(--line2);color:var(--text)}
  .bar button.ico{padding:4px 9px;font-size:13px;line-height:1;color:var(--dim)}
  .bar button.ico:hover{color:var(--text)}
  /* "off" = that panel is currently hidden */
  .bar button.ico.off{border-color:transparent;color:var(--line2)}
  .bar button.on{border-color:var(--blue);color:var(--blue)}
  kbd{font:11px var(--mono);background:var(--panel);border:1px solid var(--line);
    border-bottom-width:2px;border-radius:5px;padding:1px 5px;color:var(--sub)}

  .wrap{display:grid;grid-template-columns:minmax(0,1fr) var(--toc);gap:34px;
    padding:34px 30px 90px;max-width:1240px;width:100%;margin-inline:auto}
  /* No TOC column — centre the reading column instead of pinning it left. */
  .wrap.notoc{grid-template-columns:minmax(0,1fr)}
  .wrap.notoc .toc{display:none}
  .wrap.notoc article{margin-inline:auto}
  article{min-width:0;max-width:78ch}

  /* ── typography ───────────────────────────────────────────────────────── */
  article h1,article h2,article h3,article h4,article h5,article h6{
    line-height:1.3;font-weight:650;scroll-margin-top:70px;position:relative}
  article h1{font-size:29px;margin:6px 0 22px;letter-spacing:-.4px}
  article h2{font-size:21px;margin:44px 0 14px;padding-bottom:8px;
    border-bottom:1px solid var(--line);letter-spacing:-.2px}
  article h3{font-size:17px;margin:32px 0 10px;color:var(--text)}
  article h4{font-size:15px;margin:24px 0 8px;color:var(--sub)}
  .hlink{position:absolute;left:-20px;color:var(--line2);text-decoration:none;opacity:0;
    font-weight:400;transition:opacity .12s}
  h1:hover .hlink,h2:hover .hlink,h3:hover .hlink,h4:hover .hlink{opacity:1}
  .hlink:hover{color:var(--blue)}
  article p{margin:0 0 16px}
  article strong{color:#e9edfb;font-weight:640}
  article em{color:var(--pink);font-style:italic}
  article del{color:var(--dim)}
  article hr{border:0;border-top:1px solid var(--line);margin:34px 0}
  article a{color:var(--blue);text-decoration:none;border-bottom:1px solid rgba(137,180,250,.28)}
  article a:hover{border-bottom-color:var(--blue)}
  article a.l-src{color:var(--teal);border-bottom-color:rgba(148,226,213,.3)}
  article a.l-ext{color:var(--mauve);border-bottom-color:rgba(203,166,247,.28)}
  article a.l-miss{color:var(--red);border-bottom:1px dotted var(--red)}
  article img{max-width:100%;border-radius:10px;margin:10px 0}

  article ul,article ol{margin:0 0 16px;padding-left:24px}
  article li{margin:5px 0}
  article li::marker{color:var(--dim)}
  ul.tasks{list-style:none;padding-left:4px}
  ul.tasks li.task{display:flex;gap:9px;align-items:flex-start;margin:7px 0}
  .box{flex:none;width:16px;height:16px;border-radius:5px;border:1px solid var(--line2);
    display:inline-flex;align-items:center;justify-content:center;font-size:10px;
    margin-top:4px;color:var(--bg);font-family:var(--mono)}
  .box.done{background:var(--green);border-color:var(--green)}
  .box.open-field{border-color:var(--teal);color:var(--teal);background:transparent}
  li.task.done{color:var(--sub)}

  article code{font-family:var(--mono);font-size:.875em;background:var(--panel);
    border:1px solid var(--line);border-radius:5px;padding:1px 5px;color:var(--peach)}
  article a code{color:inherit}
  .cb{position:relative;margin:0 0 20px;background:var(--panel2);border:1px solid var(--line);
    border-radius:11px;overflow:hidden}
  .cb pre{margin:0;padding:15px 17px;overflow-x:auto;font-family:var(--mono);
    font-size:13px;line-height:1.65}
  .cb code{background:none;border:0;padding:0;color:var(--text);font-size:13px}
  .cb .lang{position:absolute;top:8px;right:60px;font:10px var(--mono);color:var(--dim);
    text-transform:uppercase;letter-spacing:.6px;pointer-events:none;max-width:55%;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .cb .lang.path{text-transform:none;letter-spacing:0;color:var(--line2)}
  .cb .copy{position:absolute;top:6px;right:8px;background:var(--panel);
    border:1px solid var(--line);color:var(--dim);border-radius:6px;padding:2px 8px;
    font:11px var(--sans);cursor:pointer;opacity:0;transition:opacity .12s}
  .cb:hover .copy{opacity:1}
  .cb .copy:hover{color:var(--text);border-color:var(--line2)}
  .cb.src{max-width:none}
  .cb.src pre{font-size:12.5px;line-height:1.6}

  .c-kw{color:var(--mauve)} .c-str{color:var(--green)} .c-num{color:var(--peach)}
  .c-cmt{color:var(--dim);font-style:italic} .c-ty{color:var(--yellow)}
  .c-fn{color:var(--blue)} .c-attr{color:var(--teal)} .c-life{color:var(--pink)}
  .c-flag{color:var(--sky)} .c-var{color:var(--sky)}

  blockquote.note{margin:0 0 18px;padding:12px 18px;border-left:3px solid var(--line2);
    background:rgba(255,255,255,.02);border-radius:0 9px 9px 0;color:var(--sub)}
  blockquote.note p:last-child{margin-bottom:0}
  blockquote.note strong{color:var(--text)}
  blockquote.tip{border-left-color:var(--green)} blockquote.warning{border-left-color:var(--yellow)}
  blockquote.important{border-left-color:var(--mauve)}
  blockquote.caution{border-left-color:var(--red)}

  .tw{overflow-x:auto;margin:0 0 20px;border:1px solid var(--line);border-radius:11px}
  table{border-collapse:collapse;width:100%;font-size:13.5px}
  th,td{padding:9px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
  th{background:var(--panel2);color:var(--sub);font-weight:600;font-size:12px;
    text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}
  tbody tr:last-child td{border-bottom:0}
  tbody tr:hover{background:rgba(255,255,255,.02)}
  td.a-right,th.a-right{text-align:right} td.a-center,th.a-center{text-align:center}
  pre.mermaid{background:var(--panel2);border:1px solid var(--line);border-radius:11px;
    padding:15px 17px;overflow-x:auto;font:12.5px/1.6 var(--mono);color:var(--sub);margin:0 0 20px}

  /* ── toc ──────────────────────────────────────────────────────────────── */
  .toc{position:sticky;top:74px;align-self:start;max-height:calc(100vh - 110px);
    overflow-y:auto;font-size:12.5px;padding-bottom:20px}
  .toc .th{color:var(--dim);text-transform:uppercase;letter-spacing:.7px;font-size:10px;
    margin-bottom:9px;font-weight:600}
  .toc a{display:block;color:var(--sub);text-decoration:none;padding:3px 0 3px 11px;
    border-left:2px solid var(--line);line-height:1.45}
  .toc a:hover{color:var(--text);border-left-color:var(--line2)}
  .toc a.on{color:var(--blue);border-left-color:var(--blue)}
  .toc a.l3{padding-left:22px;font-size:12px;color:var(--dim)}
  .toc a.l3:hover,.toc a.l3.on{color:var(--blue)}

  /* ── prev / next ──────────────────────────────────────────────────────── */
  .pn{display:flex;gap:14px;margin-top:56px;padding-top:22px;border-top:1px solid var(--line)}
  .pn a{flex:1;padding:13px 16px;border:1px solid var(--line);border-radius:11px;
    text-decoration:none;color:var(--text);border-bottom-width:1px}
  .pn a:hover{border-color:var(--line2);background:var(--panel)}
  .pn .d{display:block;font-size:11px;color:var(--dim);margin-bottom:3px;font-family:var(--mono)}
  .pn a.next{text-align:right}

  /* ── command palette ──────────────────────────────────────────────────── */
  .scrim{position:fixed;inset:0;background:rgba(17,17,27,.72);backdrop-filter:blur(3px);
    z-index:90;display:none;padding-top:11vh;justify-content:center}
  .scrim.on{display:flex}
  .pal{width:min(720px,92vw);max-height:74vh;background:var(--panel);border:1px solid var(--line2);
    border-radius:15px;overflow:hidden;display:flex;flex-direction:column;
    box-shadow:0 24px 70px rgba(0,0,0,.55)}
  .pal input{width:100%;background:transparent;border:0;border-bottom:1px solid var(--line);
    color:var(--text);padding:16px 20px;font:15px var(--sans);outline:none}
  .pal input::placeholder{color:var(--dim)}
  .pres{overflow-y:auto;padding:7px}
  .pr{display:block;padding:9px 13px;border-radius:9px;text-decoration:none;color:var(--text);
    cursor:pointer}
  .pr.sel{background:rgba(137,180,250,.14)}
  .pr .t{font-size:13.5px;display:flex;gap:9px;align-items:baseline}
  .pr .t .k{font:10px var(--mono);color:var(--dim);flex:none}
  .pr .p{font:11px var(--mono);color:var(--dim);margin-top:2px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pr .sn{font:12px var(--mono);color:var(--sub);margin-top:4px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pr .sn mark{background:rgba(249,226,175,.22);color:var(--yellow);border-radius:3px}
  .phint{padding:8px 16px;border-top:1px solid var(--line);color:var(--dim);font-size:11px;
    display:flex;gap:14px}

  article mark.flash{background:rgba(249,226,175,.28);color:inherit;border-radius:3px;
    box-shadow:0 0 0 3px rgba(249,226,175,.14);transition:all .5s}
  article mark{background:rgba(249,226,175,.2);color:inherit;border-radius:3px}
  .empty{color:var(--dim);padding:60px 0;text-align:center}
  .toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--panel);
    border:1px solid var(--line2);border-radius:10px;padding:9px 18px;font-size:12.5px;
    color:var(--sub);opacity:0;transition:opacity .2s;pointer-events:none;z-index:95}
  .toast.on{opacity:1}

  @media(max-width:1150px){
    .wrap{grid-template-columns:minmax(0,1fr)} .toc{display:none}
    article{margin-inline:auto}
  }
  @media(max-width:820px){ .app{grid-template-columns:1fr} aside{display:none} }
</style>
</head>
<body>
<div class="app" id="app">
  <aside>
    <div class="brand">
      <b>backend<span class="g">-gauntlet</span></b>
      <span class="sp" id="count"></span>
    </div>
    <div class="filter"><input id="filter" placeholder="filter files…" spellcheck="false"></div>
    <nav id="nav"></nav>
  </aside>
  <main>
    <div class="bar">
      <button class="ico" id="navBtn" title="Show/hide the file list (s)">☰</button>
      <div class="crumb" id="crumb">loading…</div>
      <span class="sp"></span>
      <button id="searchBtn">search <kbd>⌘K</kbd></button>
      <button class="ico" id="tocBtn" title="Show/hide the page outline (t)">⋮≡</button>
      <button id="zenBtn" title="Hide both panels and centre the text (\)">zen</button>
      <a class="btn" id="editBtn" href="#" title="Open this file in VS Code">edit</a>
    </div>
    <div class="wrap" id="wrap">
      <article id="doc"><div class="empty">loading…</div></article>
      <div class="toc" id="toc"></div>
    </div>
  </main>
</div>

<div class="scrim" id="scrim">
  <div class="pal">
    <input id="q" spellcheck="false"
      placeholder="Jump to a doc, or type 3+ chars to search inside all docs…">
    <div class="pres" id="pres"></div>
    <div class="phint"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span>
      <span><kbd>esc</kbd> close</span><span><kbd>s</kbd> files</span>
      <span><kbd>t</kbd> outline</span><span><kbd>\</kbd> zen</span></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = (s) => document.querySelector(s);
let TREE = [], FLAT = [], CUR = null, VERSION = "", SEL = 0, RESULTS = [], PENDING_HIT = null;

/* ── chrome (the two side panels) ────────────────────────────────────────────
   Two independent, remembered preferences. HAS_TOC is a property of the open
   document, so it is kept apart from the user's choice — otherwise navigating
   to the next doc would silently undo a hidden outline. "zen" is not a third
   state: it simply means both panels are hidden.                            */
let HIDE_NAV = localStorage.getItem("gd.hideNav") === "1";
let HIDE_TOC = localStorage.getItem("gd.hideToc") === "1";
let HAS_TOC = false;

function applyChrome(){
  const zen = HIDE_NAV && HIDE_TOC;
  $("#app").classList.toggle("nonav", HIDE_NAV);
  $("#wrap").classList.toggle("notoc", HIDE_TOC || !HAS_TOC);
  $("#navBtn").classList.toggle("off", HIDE_NAV);
  $("#tocBtn").classList.toggle("off", HIDE_TOC);
  $("#tocBtn").disabled = !HAS_TOC;
  $("#tocBtn").style.opacity = HAS_TOC ? "" : ".35";
  $("#zenBtn").classList.toggle("on", zen);
  $("#zenBtn").textContent = zen ? "exit zen" : "zen";
  localStorage.setItem("gd.hideNav", HIDE_NAV ? "1" : "");
  localStorage.setItem("gd.hideToc", HIDE_TOC ? "1" : "");
}

function toggleNav(){ HIDE_NAV = !HIDE_NAV; applyChrome(); }
function toggleToc(){ if (HAS_TOC){ HIDE_TOC = !HIDE_TOC; applyChrome(); } }
/* Zen hides both; pressing it again brings both back. */
function toggleZen(){
  const zen = HIDE_NAV && HIDE_TOC;
  HIDE_NAV = HIDE_TOC = !zen;
  applyChrome();
}

/* ── data ───────────────────────────────────────────────────────────────── */
async function loadTree(){
  const r = await fetch("/api/tree").then(r => r.json());
  TREE = r.groups; VERSION = r.version;
  FLAT = [];
  for (const g of TREE) for (const d of g.docs) FLAT.push({...d, group: g.title, gid: g.id});
  $("#count").textContent = FLAT.length + " docs";
  renderNav();
}

function renderNav(){
  const filter = $("#filter").value.trim().toLowerCase();
  const nav = $("#nav");
  nav.innerHTML = "";
  const saved = JSON.parse(localStorage.getItem("gd.open") || "{}");
  for (const g of TREE){
    const docs = filter
      ? g.docs.filter(d => (d.title + " " + d.path + " " + d.label).toLowerCase().includes(filter))
      : g.docs;
    if (!docs.length) continue;
    const isOpen = filter ? true : (saved[g.id] ?? defaultOpen(g));
    const div = document.createElement("div");
    div.className = "grp" + (isOpen ? " open" : "");
    div.innerHTML =
      `<div class="ghead${isOpen ? " open" : ""}"><span class="chev">▶</span>` +
      (g.num ? `<span class="num">${g.num}</span>` : "") +
      `<span>${esc(g.title)}</span>` +
      (g.state ? `<span class="st ${esc(g.state)}" title="${esc(g.state)}"></span>` : "") +
      `</div><div class="gitems">` +
      docs.map(d =>
        `<a class="item k${d.kind}${d.path === CUR ? " on" : ""}" href="#/${d.path}">` +
        `<span class="k">${d.kind === "doc" ? "·" : d.kind[0]}</span>` +
        `<span>${esc(d.label)}</span></a>`).join("") +
      `</div>`;
    div.querySelector(".ghead").onclick = () => {
      const now = !div.classList.contains("open");
      div.classList.toggle("open", now);
      div.querySelector(".ghead").classList.toggle("open", now);
      const st = JSON.parse(localStorage.getItem("gd.open") || "{}");
      st[g.id] = now; localStorage.setItem("gd.open", JSON.stringify(st));
    };
    nav.appendChild(div);
  }
}
const gPrefix = (g) => g.id.startsWith("p") ? "projects/" + g.id.slice(1) + "/" : null;
/* Open the group holding the current doc; otherwise only the repo-root group. */
const defaultOpen = (g) => {
  const prefix = gPrefix(g);
  return Boolean(prefix && CUR && CUR.startsWith(prefix)) || g.id === "root";
};
const ESCAPES = {"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;"};
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ESCAPES[c]);

/* ── routing ────────────────────────────────────────────────────────────── */
function route(){
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  if (!h){
    const last = localStorage.getItem("gd.last");
    if (last){ location.hash = "#/" + last; return; }
    return openDoc("README.md");
  }
  const [path, anchor] = h.split("::");
  openDoc(path, anchor);
}

async function openDoc(path, anchor, keepScroll){
  const scroll = keepScroll ? window.scrollY : 0;
  const d = await fetch("/api/doc?p=" + encodeURIComponent(path)).then(r => r.json());
  if (d.error){
    $("#doc").innerHTML = `<div class="empty">${esc(d.error)}</div>`;
    $("#crumb").textContent = path;
    HAS_TOC = false; $("#toc").innerHTML = ""; applyChrome();
    return;
  }
  CUR = d.path;
  localStorage.setItem("gd.last", d.path);
  document.title = d.title + " · gauntlet docs";
  const parts = d.path.split("/");
  $("#crumb").innerHTML = parts.map((p, i) =>
    i === parts.length - 1 ? `<b>${esc(p)}</b>` : esc(p)).join(" / ");
  $("#editBtn").href = "vscode://file/" + d.abs;
  $("#doc").innerHTML = d.html + prevNext(d.path);
  buildToc(d.toc);
  wireCode();
  if (d.mermaid) loadMermaid();
  renderNav();
  document.querySelector(".item.on")?.scrollIntoView({block:"nearest"});
  if (keepScroll) window.scrollTo(0, scroll);
  else if (anchor){
    const el = document.getElementById(anchor);
    el ? el.scrollIntoView() : window.scrollTo(0, 0);
  } else window.scrollTo(0, 0);
  if (PENDING_HIT){ const q = PENDING_HIT; PENDING_HIT = null; scrollToText(q); }
}

/* After opening a full-text hit, land on the match instead of the page top. */
function scrollToText(q){
  const needle = q.toLowerCase();
  const walk = document.createTreeWalker($("#doc"), NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walk.nextNode())){
    const at = node.textContent.toLowerCase().indexOf(needle);
    if (at < 0) continue;
    const range = document.createRange();
    range.setStart(node, at);
    range.setEnd(node, at + needle.length);
    const mark = document.createElement("mark");
    mark.className = "flash";
    try { range.surroundContents(mark); } catch(_) { return; }
    mark.scrollIntoView({block: "center"});
    setTimeout(() => mark.classList.remove("flash"), 2200);
    return;
  }
}

function prevNext(path){
  const i = FLAT.findIndex(d => d.path === path);
  if (i < 0) return "";
  const p = FLAT[i - 1], n = FLAT[i + 1];
  let h = '<div class="pn">';
  const gap = "<span style='flex:1'></span>";
  h += p ? `<a href="#/${p.path}"><span class="d">← previous</span>${esc(p.title)}</a>` : gap;
  h += n ? `<a class="next" href="#/${n.path}"><span class="d">next →</span>${esc(n.title)}</a>`
         : gap;
  return h + "</div>";
}

function buildToc(toc){
  const el = $("#toc");
  HAS_TOC = Boolean(toc && toc.length >= 2);
  if (!HAS_TOC){ el.innerHTML = ""; applyChrome(); return; }
  applyChrome();
  el.innerHTML = '<div class="th">on this page</div>' + toc.map(t =>
    `<a class="l${t.level}" href="#${t.id}" data-id="${t.id}">${esc(t.text)}</a>`).join("");
  el.querySelectorAll("a").forEach(a => a.onclick = (e) => {
    e.preventDefault();
    document.getElementById(a.dataset.id)?.scrollIntoView();
  });
  observeHeadings(toc);
}

let OBS = null;
function observeHeadings(toc){
  OBS?.disconnect();
  const links = new Map([...$("#toc").querySelectorAll("a")].map(a => [a.dataset.id, a]));
  OBS = new IntersectionObserver((entries) => {
    for (const e of entries){
      if (!e.isIntersecting) continue;
      links.forEach(a => a.classList.remove("on"));
      links.get(e.target.id)?.classList.add("on");
      break;
    }
  }, {rootMargin: "-70px 0px -75% 0px"});
  toc.forEach(t => { const el = document.getElementById(t.id); if (el) OBS.observe(el); });
}

function wireCode(){
  document.querySelectorAll(".cb .copy").forEach(b => b.onclick = () => {
    navigator.clipboard.writeText(b.parentElement.querySelector("code").innerText);
    toast("copied");
  });
}

let toastT;
function toast(msg){
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("on"), 1400);
}

/* Mermaid is the one thing we cannot render offline — try the CDN, and if it is
   not reachable the diagram just stays readable as its source text. */
let mermaidTried = false;
function loadMermaid(){
  if (window.mermaid){ window.mermaid.run({querySelector:"pre.mermaid"}); return; }
  if (mermaidTried) return;
  mermaidTried = true;
  const s = document.createElement("script");
  s.type = "module";
  s.textContent =
    'import m from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";' +
    'window.mermaid = m; m.initialize({startOnLoad:false, theme:"dark",' +
    'themeVariables:{background:"#11111b",primaryColor:"#1e1e2e",lineColor:"#6c7086",' +
    'primaryTextColor:"#cdd6f4",primaryBorderColor:"#45475a"}});' +
    'm.run({querySelector:"pre.mermaid"});';
  document.head.appendChild(s);
}

/* ── command palette ────────────────────────────────────────────────────── */
function fuzzy(q, s){
  q = q.toLowerCase(); s = s.toLowerCase();
  let qi = 0, score = 0, streak = 0;
  for (let i = 0; i < s.length && qi < q.length; i++){
    if (s[i] === q[qi]){ qi++; streak++; score += streak * 2 + (i === 0 ? 6 : 0); }
    else streak = 0;
  }
  return qi === q.length ? score - s.length * 0.04 : -1;
}

let searchT;
function palette(show){
  $("#scrim").classList.toggle("on", show);
  if (show){ $("#q").value = ""; $("#q").focus(); paletteRender(""); }
}

function paletteRender(q){
  const list = $("#pres");
  if (!q){
    RESULTS = FLAT.slice(0, 40).map(d => ({...d, _hit: false}));
  } else {
    RESULTS = FLAT
      .map(d => ({d, s: Math.max(fuzzy(q, d.title), fuzzy(q, d.path))}))
      .filter(x => x.s > 0).sort((a, b) => b.s - a.s).slice(0, 25)
      .map(x => ({...x.d, _hit: false}));
  }
  SEL = 0;
  list.innerHTML = RESULTS.map((r, i) => rowHtml(r, i)).join("") ||
    '<div class="empty" style="padding:30px">no matches</div>';
  wireRows();
  if (q.length >= 3){
    clearTimeout(searchT);
    searchT = setTimeout(async () => {
      const r = await fetch("/api/search?q=" + encodeURIComponent(q)).then(r => r.json());
      if ($("#q").value.trim() !== q) return;
      const hits = r.results.map(x => ({...x, _hit: true}));
      RESULTS = RESULTS.concat(hits);
      list.innerHTML = RESULTS.map((r, i) => rowHtml(r, i, q)).join("") ||
        '<div class="empty" style="padding:30px">no matches</div>';
      wireRows();
    }, 160);
  }
}

function rowHtml(r, i, q){
  const sel = i === SEL ? " sel" : "";
  if (r._hit){
    let sn = esc(r.snippet);
    if (q){
      const re = new RegExp("(" + q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
      sn = sn.replace(re, "<mark>$1</mark>");
    }
    return `<div class="pr${sel}" data-i="${i}"><div class="t"><span class="k">↳</span>` +
      `<span>${esc(r.title)}</span></div><div class="sn">${sn}</div>` +
      `<div class="p">${esc(r.path)}:${r.line}</div></div>`;
  }
  return `<div class="pr${sel}" data-i="${i}"><div class="t">` +
    `<span class="k">${esc((r.label || "").slice(0, 5))}</span>` +
    `<span>${esc(r.title)}</span></div><div class="p">${esc(r.path)}</div></div>`;
}

function wireRows(){
  document.querySelectorAll(".pr").forEach(el => {
    el.onclick = () => choose(+el.dataset.i);
    el.onmouseenter = () => { SEL = +el.dataset.i; highlight(); };
  });
}
function highlight(){
  document.querySelectorAll(".pr").forEach(el =>
    el.classList.toggle("sel", +el.dataset.i === SEL));
  document.querySelector(".pr.sel")?.scrollIntoView({block:"nearest"});
}
function choose(i){
  const r = RESULTS[i];
  if (!r) return;
  const q = $("#q").value.trim();
  palette(false);
  if (r._hit && q) PENDING_HIT = q;
  const target = "#/" + r.path;
  // Setting an identical hash fires no hashchange — route by hand in that case.
  if (location.hash === target) route();
  else location.hash = target;
}

/* ── events ─────────────────────────────────────────────────────────────── */
$("#filter").oninput = renderNav;
$("#searchBtn").onclick = () => palette(true);
$("#zenBtn").onclick = toggleZen;
$("#navBtn").onclick = toggleNav;
$("#tocBtn").onclick = toggleToc;
$("#q").oninput = (e) => paletteRender(e.target.value.trim());
$("#scrim").onclick = (e) => { if (e.target === $("#scrim")) palette(false); };

document.addEventListener("keydown", (e) => {
  const palOpen = $("#scrim").classList.contains("on");
  const typing = /input|textarea/i.test(document.activeElement.tagName);
  if ((e.ctrlKey || e.metaKey) && e.key === "k"){ e.preventDefault(); palette(!palOpen); return; }
  if (palOpen){
    if (e.key === "Escape") palette(false);
    else if (e.key === "ArrowDown"){
      e.preventDefault(); SEL = Math.min(SEL + 1, RESULTS.length - 1); highlight(); }
    else if (e.key === "ArrowUp"){ e.preventDefault(); SEL = Math.max(SEL - 1, 0); highlight(); }
    else if (e.key === "Enter"){ e.preventDefault(); choose(SEL); }
    return;
  }
  if (typing){ if (e.key === "Escape") document.activeElement.blur(); return; }
  if (e.key === "/"){ e.preventDefault(); palette(true); }
  else if (e.key === "\\"){ toggleZen(); }
  else if (e.key === "s"){ toggleNav(); }
  else if (e.key === "t"){ toggleToc(); }
  else if (e.key === "Escape" && (HIDE_NAV || HIDE_TOC)){
    HIDE_NAV = HIDE_TOC = false; applyChrome();
  }
  else if (e.key === "[" || e.key === "]"){
    const i = FLAT.findIndex(d => d.path === CUR);
    const t = FLAT[i + (e.key === "]" ? 1 : -1)];
    if (t) location.hash = "#/" + t.path;
  }
});

window.addEventListener("hashchange", route);

/* live reload — repoll mtimes; re-render the open doc in place when it changes */
setInterval(async () => {
  try{
    const r = await fetch("/api/version").then(r => r.json());
    if (r.version !== VERSION){
      VERSION = r.version;
      await loadTree();
      if (CUR) await openDoc(CUR, null, true);
      toast("reloaded");
    }
  }catch(_){}
}, 2000);

(async () => { applyChrome(); await loadTree(); route(); })();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
