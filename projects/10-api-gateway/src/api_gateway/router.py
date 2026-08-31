"""V2 — The request routing engine.

Module: `src/api_gateway/router.py`. Maps an inbound `(host, path, method)` to a
route, and thus to an upstream pool. The scaffold compiles the table from config
(plumbing); `Router.match_request` is yours.

The naive version is one line and it is what everybody writes first:

    next((r for r in self.routes if path.startswith(r.path_prefix)), None)

It is wrong twice. It is **O(routes)** on the hot path of *every* request, which
is the one place in this whole service where a constant factor is multiplied by
your entire traffic. And it is **ambiguous**: with `/api` and `/api/v2` both
registered, `/api/v2/users` matches whichever happens to come first in the list,
so your routing depends on the order somebody typed the config file in. V2 wants
longest-prefix precedence that is a property of the *table*, not of its order.

## Re-aiming the hint for Python

Two structures do this well here, and they trade off differently:

* **A trie over path segments.** Split the path on `/` and walk nested `dict`s
  one segment at a time, remembering the deepest node that carried a route.
  Lookup is O(segments) — the depth of the path, not the size of the table — so
  it does not care whether you have 10 routes or 10,000. Dict lookup is a C-level
  hash, so the constant is small.
* **A sorted list of prefixes plus `bisect`.** Keep prefixes sorted and use
  `bisect.bisect_right` to land next to the best candidate, then walk back a
  short way. O(log routes), less code, and it reuses a stdlib module that is worth
  knowing.

Either satisfies the SPEC. What will not is a linear scan dressed up — and this
is the specific way Python misleads you here: `str.startswith` runs in C, so
scanning a few hundred routes benchmarks *fine*. The criterion is asymptotic, and
the bench V2 asks for (10 -> 10k routes) is the thing that tells the two apart.

## Two things that bite in this file

**Prefix matching is not string matching.** `/api` must not match `/apiary`. A
bare `startswith` says it does. Match on **segment boundaries**: either the path
equals the prefix or it continues with a `/`. This is a real production incident
in miniature — traffic for a new service silently swallowed by an older route
with a shorter name.

**Swapping the table is easier than it looks.** V2's last criterion — rebuild the
route table without dropping in-flight requests — has an anticlimactic answer on
one event loop: build the *new* `Router` completely, off the request path, then
rebind the single attribute that points at it. Attribute assignment can't be
interrupted by the loop, so every request either sees the whole old table or the
whole new one, never a half-built one, and requests already in flight keep the
object they started with until they finish and it is garbage-collected. No lock,
no drain. What it does require is that a `Router` be **immutable in practice**
after `build` — the moment reload starts mutating a live table in place, that
guarantee is gone.
"""

from __future__ import annotations

from .balancer import Backend, Balancer
from .config import GatewayConfig
from .health import CircuitBreaker

__all__ = ["Route", "Router", "Upstream"]


class Upstream:
    """A resolved upstream: a named pool with its balancer (V3)."""

    __slots__ = ("balancer", "name")

    def __init__(self, name: str, balancer: Balancer) -> None:
        self.name = name
        self.balancer = balancer


class Route:
    """One compiled route: a match rule plus the upstream to forward to."""

    __slots__ = ("host", "methods", "name", "path_prefix", "upstream")

    def __init__(
        self,
        name: str,
        host: str | None,
        path_prefix: str,
        methods: frozenset[str],
        upstream: Upstream,
    ) -> None:
        self.name = name
        self.host = host
        """Host constraint (`None` = any host)."""
        self.path_prefix = path_prefix
        """Longest-prefix match target, e.g. `/api/v2`."""
        self.methods = methods
        """Allowed methods, uppercase. Empty = any.

        A `frozenset` rather than a list because the matcher asks "is this method
        allowed" once per request: set membership is a hash, list membership is a
        scan, and this sits on the hot path next to everything else V2 is trying
        to keep sub-linear."""
        self.upstream = upstream

    def __repr__(self) -> str:
        return f"Route({self.name!r}, prefix={self.path_prefix!r}, host={self.host!r})"


class Router:
    """The route table.

    Treat an instance as immutable once built — see the module docstring on why
    that is what makes config reload safe.
    """

    def __init__(self, routes: list[Route]) -> None:
        self.routes = routes
        # TODO(V2): build your matching structure here, once, from `routes`.
        # Everything expensive belongs at build time; `match_request` should do
        # nothing but walk it.

    @classmethod
    def build(
        cls, cfg: GatewayConfig, *, failure_threshold: int = 5, open_cooldown: float = 5.0
    ) -> Router:
        """Compile the config into the route table (plumbing).

        Each route's backends become a pool behind a `Balancer`, and each backend
        gets its own circuit breaker carrying the operator's thresholds (V4). The
        *matching structure* is yours to build — see `__init__`.
        """
        routes: list[Route] = []
        for rc in cfg.routes:
            backends = [
                Backend(addr, CircuitBreaker(failure_threshold, open_cooldown))
                for addr in rc.upstream.backends
            ]
            routes.append(
                Route(
                    name=rc.name,
                    host=rc.host,
                    path_prefix=rc.path_prefix,
                    methods=frozenset(rc.methods),
                    upstream=Upstream(rc.name, Balancer(rc.upstream.lb, backends)),
                )
            )
        return cls(routes)

    def route_names(self) -> list[str]:
        """Route names, for `GET /admin/routes`."""
        return [r.name for r in self.routes]

    def backends(self) -> list[Backend]:
        """Every backend across every route — the fleet the active health checker
        (V4) probes.

        Note that this can contain the same address twice if two routes name it;
        whether that means two independent circuit breakers or one shared view of
        that backend's health is a real design decision, and `docs/10-design.md`
        is where you record which you chose."""
        return [b for r in self.routes for b in r.upstream.balancer.backends]

    def match_request(self, host: str | None, path: str, method: str) -> Route | None:
        """Resolve a request to a route, or `None` (-> 404 no route).

        TODO(V2): match on `host` + longest `path_prefix` + `method`, using the
        structure you built in `__init__`.

        The four rules, in the order they trip people up:
          * **Longest prefix wins**, deterministically — not by insertion order,
            and not by which one you happened to test first.
          * **Segment boundaries**, not raw string prefixes: `/api` matches
            `/api` and `/api/v2/x`, never `/apiary`.
          * **Host and method are filters, not tie-breaks.** A route scoped to
            `POST` does not match a `GET`; a route scoped to `api.example.com`
            does not match another host. The interesting case is when the longest
            prefix is *excluded* by its host or method constraint and a shorter
            one would have matched — decide whether that falls through to the
            shorter route or 404s, and write the answer in `docs/10-design.md`.
            Both are defensible; silently doing one of them is not.
          * **`Host` may carry a port** (`api.example.com:8443`) and is
            case-insensitive. Normalize both sides or you will match in
            development and not in production.

        `method` arrives uppercase from the HTTP layer, and `RouteConfig`
        uppercases the configured ones at load, so no case handling is needed on
        that axis.
        """
        raise NotImplementedError(
            "V2: match (host, path prefix, method) -> route; longest-prefix wins"
        )
