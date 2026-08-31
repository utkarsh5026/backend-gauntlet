"""Security checklist - per-key rate limiting on the write path.

Time is injected rather than slept, so these are deterministic and instant.
"""

from __future__ import annotations

import pytest

from url_shortener.errors import RateLimited
from url_shortener.ratelimit import RateLimiter, enforce_rate_limit


def test_allows_exactly_the_burst_then_denies() -> None:
    limiter = RateLimiter(period=0.2, burst=10)
    allowed = [limiter.check("k1", now=0.0).allowed for _ in range(11)]
    assert allowed == [True] * 10 + [False]


def test_remaining_counts_down_to_zero() -> None:
    limiter = RateLimiter(period=0.2, burst=5)
    remaining = [limiter.check("k1", now=0.0).remaining for _ in range(5)]
    assert remaining == [4, 3, 2, 1, 0]


def test_budget_refills_at_one_per_period() -> None:
    limiter = RateLimiter(period=0.2, burst=3)
    for _ in range(3):
        assert limiter.check("k1", now=0.0).allowed
    assert not limiter.check("k1", now=0.0).allowed

    # One period later, exactly one request is affordable again.
    assert limiter.check("k1", now=0.2).allowed
    assert not limiter.check("k1", now=0.2).allowed


def test_buckets_are_independent_per_key() -> None:
    """The whole point of keying on the API key: one noisy caller must not
    spend anybody else's budget."""
    limiter = RateLimiter(period=0.2, burst=1)
    assert limiter.check("k1", now=0.0).allowed
    assert not limiter.check("k1", now=0.0).allowed
    assert limiter.check("k2", now=0.0).allowed


def test_denial_reports_a_retry_after() -> None:
    limiter = RateLimiter(period=0.2, burst=2)
    for _ in range(2):
        limiter.check("k1", now=0.0)
    decision = limiter.check("k1", now=0.0)

    assert not decision.allowed
    assert decision.retry_after > 0
    headers = decision.headers()
    # Whole seconds, rounded up - a sub-second wait must not round to "now".
    assert headers["retry-after"] == "1"
    assert headers["x-ratelimit-limit"] == "2"
    assert headers["x-ratelimit-remaining"] == "0"


def test_rejects_a_nonsense_configuration() -> None:
    with pytest.raises(ValueError, match="positive period"):
        RateLimiter(period=0.0, burst=10)
    with pytest.raises(ValueError, match="positive period"):
        RateLimiter(period=0.2, burst=0)


def test_pruning_forgets_fully_refilled_keys() -> None:
    """Without this the limiter is a memory leak keyed by attacker input."""
    limiter = RateLimiter(period=0.2, burst=1)
    for index in range(20_000):
        limiter.check(f"key-{index}", now=0.0)
    # The prune runs once past the threshold; every key charged at t=0 has a TAT
    # of 0.2, so by t=100 none of them are worth remembering.
    limiter.check("late", now=100.0)
    assert limiter.check("key-0", now=100.0).remaining == 0


async def test_enforce_raises_with_headers_attached() -> None:
    limiter = RateLimiter(period=0.2, burst=1)
    assert await enforce_rate_limit("k1", limiter) is not None

    with pytest.raises(RateLimited) as caught:
        await enforce_rate_limit("k1", limiter)
    assert caught.value.headers is not None
    assert "retry-after" in caught.value.headers
