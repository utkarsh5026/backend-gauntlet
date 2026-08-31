"""V3's backoff curve — pure, so it needs no database.

The curve is the part of the retry policy that has to be *reasoned* about rather
than observed: it decides whether a wave of simultaneous failures comes back as a
thundering herd or as a spread-out trickle.
"""

from __future__ import annotations

from job_queue.retry import RetryPolicy


def test_ceiling_grows_then_caps_at_max_delay() -> None:
    p = RetryPolicy()
    assert p.ceiling(1) == p.base_delay, "attempt 1's ceiling is the base"
    assert p.ceiling(2) == p.base_delay * 2
    assert p.ceiling(64) == p.max_delay
    assert p.ceiling(1000) == p.max_delay, "a large attempt saturates, it does not overflow"


def test_ceiling_is_non_decreasing_up_to_the_cap() -> None:
    """The curve never goes *backwards*.

    This is precisely the property that a jitter scaled to `max_delay` rather than
    to the current ceiling silently violates — it makes an early attempt able to
    wait longer than a later one, which is not a backoff at all.
    """
    p = RetryPolicy()
    previous = 0.0
    for attempt in range(1, 40):
        current = p.ceiling(attempt)
        assert current >= previous, f"ceiling dropped at attempt {attempt}"
        previous = current


def test_backoff_stays_within_the_ceiling() -> None:
    p = RetryPolicy()
    for attempt in range(1, 12):
        ceiling = p.ceiling(attempt)
        for _ in range(50):
            assert 0.0 <= p.backoff(attempt) <= ceiling


def test_backoff_applies_jitter() -> None:
    """Two draws at the same attempt must not always agree.

    Full jitter is the whole point: without it every worker that failed at the same
    instant retries at the same instant, and the herd re-collides at `2^n`.
    """
    p = RetryPolicy()
    draws = {p.backoff(8) for _ in range(64)}
    assert len(draws) > 1, "backoff returned a constant — the jitter is missing"


def test_backoff_never_exceeds_max_delay() -> None:
    p = RetryPolicy()
    for attempt in (1, 10, 100, 10_000):
        for _ in range(50):
            assert p.backoff(attempt) <= p.max_delay
