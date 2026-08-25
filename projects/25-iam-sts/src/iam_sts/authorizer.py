"""V5 — The authorization hot path: every request in the company waits here.

IAM's p99 is a floor under every other service's p99. Project 23 cannot answer a
`GetItem` faster than this answers "may they?", and project 24 cannot start an
invocation before it. Nothing in the fleet gets to be faster than authorization,
which makes the whole game *doing less work per decision*:

  * **Compile once.** A policy is JSON on disk and a matcher in memory. Walking
    JSON per request pays the parse over and over for a document that changed
    last Tuesday.
  * **Cache the decision, not the documents.** Caching policies still leaves you
    evaluating five layers per request. Caching the decision skips all of it.
  * **Cache denials too.** If misses are slower than hits and denials always
    miss, then denials are always slow — and an attacker sending garbage gets a
    cheap amplification factor against you for free.
  * **Bound it, and evict fairly.** One account enumerating novel resource names
    must not evict the working set every other account depends on.

And then the part that makes this a *security* vertical rather than a
performance one:

    A cached allow that outlives the policy change which revoked it is a
    vulnerability whose severity is measured in seconds of TTL.

`decision_cache_ttl_seconds` is not a tuning knob. It is a written promise about
the maximum time a permission you already deleted still works, and the SPEC asks
you to state it in those words and then verify it under load.

Scaffold state: the shapes and the counters are here; compilation, caching and
the decision raise.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .config import Settings
from .evaluation import PolicyEvaluator, PolicySet
from .models import AuthorizationRequest, AuthorizationResult
from .policy import PolicyDocument

__all__ = ["Authorizer", "CacheStats", "CompiledPolicy", "DecisionCache", "PolicyCompiler"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CacheStats:
    """What `/metrics` reports and what the boss fight measures.

    `hits / (hits + misses)` is the hit ratio with an explicit boss-fight target,
    so it is worth being able to read at any instant rather than reconstructing
    it from a histogram afterwards.
    """

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    invalidations: int = 0
    entries: int = 0

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


@dataclass(slots=True)
class CompiledPolicy:
    """A policy prepared for the hot path.

    What goes in here is the design decision of this vertical, and it is worth
    thinking about before writing it. The naive answer is "the parsed statements",
    which is barely faster than re-parsing. The interesting answers pre-compute
    the things the matcher does per request: the set of service prefixes a policy
    can possibly match (so a `s3:*` policy is skipped instantly for a `dynamodb:`
    action), statements bucketed by effect so denies can be checked first, and
    patterns pre-split into segments so V2's matcher does no string work at all.

    `source_version` exists so a compiled artifact can be checked against the
    document it came from — a compiled cache that cannot tell it is stale is the
    bug this whole vertical is trying not to have.
    """

    policy_id: str
    source_version: int
    # TODO(V5): whatever the compiled form turns out to be. Keep it immutable —
    # it is shared across every concurrent request touching this policy, and a
    # mutable one is a data race in the one place a data race means "wrong
    # authorization decision" rather than "wrong number in a dashboard".


class PolicyCompiler:
    """Turns a `PolicyDocument` into a `CompiledPolicy`, off the request path."""

    def __init__(self, settings: Settings) -> None:
        self._max_entries = settings.compiled_policy_cache_size
        self._compiled: dict[str, CompiledPolicy] = {}

    def compile(self, document: PolicyDocument, version: int) -> CompiledPolicy:
        """Compile (and memoize) one policy."""
        # TODO(V5): compile, store, evict when over the bound.
        #
        # "Off the request path" is a criterion, so decide where this runs: at
        # write time in the control plane is the honest answer, with the hot path
        # only ever *reading* an already-compiled artifact. Compiling lazily on
        # first use is easier and moves a multi-millisecond spike onto whichever
        # unlucky request arrives first after a change — which, right after a
        # deploy, is all of them at once.
        raise NotImplementedError("V5: compile a policy for the hot path")

    def invalidate(self, policy_id: str) -> None:
        """Drop a compiled policy after its document changed."""
        # TODO(V5): drop it, and make sure the decision cache hears about it too —
        # a stale *decision* outlives a stale compilation, so evicting only this
        # one fixes nothing observable.
        raise NotImplementedError("V5: invalidate a compiled policy")


class DecisionCache:
    """Bounded, TTL'd cache of authorization decisions.

    The cache key is the hard part, and it is a correctness problem rather than a
    performance one. It must cover **every input the decision depended on**:
    principal, action, resource, *and each condition key that was actually
    consulted*. Miss one — say a policy conditioning on `aws:SourceIp` — and the
    first request from one IP poisons the answer for every other IP, which is a
    security bug that presents as "it works on my machine".

    The reason the result carries `consulted_context_keys` is precisely so this
    class can build a complete key without knowing anything about policies.
    """

    def __init__(self, settings: Settings) -> None:
        self._max_entries = settings.decision_cache_size
        self._ttl = settings.decision_cache_ttl_seconds
        self.stats = CacheStats()

    def get(self, request: AuthorizationRequest) -> AuthorizationResult | None:
        """Look up a cached decision, or None."""
        # TODO(V5): build the key, check the TTL, count the hit or the miss.
        #
        # The chicken-and-egg to solve: the key needs the consulted condition
        # keys, and you only know those *after* evaluating. Two shapes work — key
        # on the full context (correct, but a low hit ratio when the context
        # carries anything request-specific like a timestamp), or key in two
        # stages (look up which keys this principal+action+resource consults, then
        # key on those). The second is faster and has a subtle invalidation
        # requirement: the set of consulted keys itself changes when a policy
        # changes. Pick one, write down why, and make the tests prove it.
        raise NotImplementedError("V5: look up a cached decision")

    def put(self, request: AuthorizationRequest, result: AuthorizationResult) -> None:
        """Cache a decision — allow or deny, both."""
        # TODO(V5): insert and evict. Cache denials with the same care as allows:
        # the SPEC measures the deny path against the allow path, and a design
        # where denials always miss is a design where an attacker chooses your
        # latency.
        #
        # Eviction policy is a criterion: plain LRU lets one principal touching a
        # million novel resources evict everything. Segmenting by principal, or
        # an admission policy that only caches on second sighting, are the usual
        # answers — the boss fight reproduces the attack, so pick something that
        # survives it.
        raise NotImplementedError("V5: cache a decision")

    def invalidate_principal(self, principal_arn: str) -> int:
        """Drop every cached decision for one principal. Returns how many."""
        # TODO(V5): this is where the propagation-window criterion is won or lost.
        #
        # Precision matters both ways. Flushing the whole cache is correct and
        # catastrophic — the hit ratio goes to zero and every request in the
        # account takes the miss path at once, which is a self-inflicted
        # thundering herd on a policy edit. Flushing too little leaves a revoked
        # permission working. An index from principal → cached keys is the usual
        # shape, and it is memory you spend to buy a precise invalidation.
        raise NotImplementedError("V5: invalidate one principal's cached decisions")

    def __len__(self) -> int:
        return self.stats.entries


class Authorizer:
    """The hot path: assemble the policies, decide, cache, and answer.

    Deliberately thin. All the *judgement* is in V3's evaluator, which is pure
    and testable; everything here is about not doing that work twice. Keeping the
    split means a cache bug cannot silently become a policy bug — and V6's
    simulator can call the evaluator directly and get an answer that provably
    matches this one.
    """

    def __init__(
        self,
        settings: Settings,
        evaluator: PolicyEvaluator,
        compiler: PolicyCompiler,
        cache: DecisionCache,
    ) -> None:
        self._settings = settings
        self._evaluator = evaluator
        self._compiler = compiler
        self._cache = cache

    async def authorize(
        self, request: AuthorizationRequest, policies: PolicySet
    ) -> AuthorizationResult:
        """Answer one authorization question."""
        # TODO(V5): cache lookup, then evaluate on a miss, then cache the result.
        #
        # Mark the result `cached` so the caller — and the latency histogram —
        # can tell hits from misses. The SPEC requires hit and miss p99 reported
        # separately, and that is impossible after the fact if the result does
        # not say which it was.
        #
        # Two failure modes worth designing against up front:
        #
        #  * **Stampede.** N concurrent identical misses evaluate N times. A
        #    single-flight per key collapses them, and the cold phase of the boss
        #    fight is built to expose exactly this.
        #  * **Failing open.** If the evaluation raises, the answer is deny —
        #    never "allow because the cache was empty and something went wrong".
        #    Under a partial failure an authorization service must get *slower or
        #    stricter*, never more permissive.
        raise NotImplementedError("V5: authorize, using and maintaining the decision cache")

    def stats(self) -> CacheStats:
        """For `/metrics`. Plumbing."""
        return self._cache.stats
