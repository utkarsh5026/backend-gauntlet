"""The bearer check.

The parsing plus the constant-time compare is the whole security-relevant surface;
the dependency around it is thin glue, exercised end-to-end in `test_routes`.
"""

from __future__ import annotations

from job_queue.routes import bearer_matches

TOKEN = "s3cret-abc123"


def test_accepts_the_correct_bearer_token() -> None:
    assert bearer_matches(f"Bearer {TOKEN}", TOKEN)


def test_rejects_wrong_token_same_and_different_length() -> None:
    assert not bearer_matches("Bearer s3cret-abc124", TOKEN)  # one byte off
    assert not bearer_matches("Bearer nope", TOKEN)  # shorter


def test_rejects_missing_or_wrong_scheme() -> None:
    assert not bearer_matches(TOKEN, TOKEN)  # no "Bearer " prefix
    assert not bearer_matches(f"Basic {TOKEN}", TOKEN)  # wrong scheme
    assert not bearer_matches(f"bearer {TOKEN}", TOKEN)  # case-sensitive


def test_rejects_absent_header_and_empty_token() -> None:
    assert not bearer_matches(None, TOKEN)
    assert not bearer_matches("Bearer ", TOKEN)  # empty provided
    assert not bearer_matches(f"Bearer {TOKEN}", "")  # empty expected
