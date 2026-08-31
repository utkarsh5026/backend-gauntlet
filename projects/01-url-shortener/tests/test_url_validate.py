"""Security checklist - URL validation and SSRF rejection.

Each rejection rule gets its own case, because "the validator rejects bad input"
is not a claim you can check; "it rejects `https://169.254.169.254/`" is.
"""

from __future__ import annotations

import pytest

from url_shortener.errors import BadRequest
from url_shortener.url_validate import MAX_URL_LEN, validate_long_url


def test_accepts_public_https_url() -> None:
    assert validate_long_url("https://example.com/path?q=1") == "https://example.com/path?q=1"


def test_trims_whitespace_and_adds_root_path() -> None:
    assert validate_long_url("  https://example.com  ") == "https://example.com/"


def test_strips_fragment_and_default_port() -> None:
    """Normalization so equivalent inputs dedupe to one stored string. The
    fragment is never sent on a redirect anyway."""
    assert validate_long_url("https://example.com:443/x#section") == "https://example.com/x"


def test_keeps_a_non_default_port() -> None:
    assert validate_long_url("https://example.com:8443/x") == "https://example.com:8443/x"


def test_allows_http_inside_the_query_string() -> None:
    """Only the *scheme* has to be https - a `next=` parameter is opaque data."""
    assert (
        validate_long_url("https://example.com?next=http://other.example")
        == "https://example.com/?next=http://other.example"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "ftp://example.com/x",
    ],
)
def test_rejects_non_https_schemes(url: str) -> None:
    with pytest.raises(BadRequest):
        validate_long_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/",
        "https://192.168.1.1/",
        "https://10.0.0.1/",
        "https://172.16.0.1/",
        "https://169.254.169.254/",  # the cloud metadata endpoint
        "https://100.64.0.1/",  # CGNAT
        "https://0.0.0.0/",
        "https://[::1]/",
        "https://[fc00::1]/",  # unique-local
        "https://[fe80::1]/",  # link-local
        "https://[::ffff:127.0.0.1]/",  # loopback wearing an IPv6 costume
        "https://localhost/",
        "https://LOCALHOST./",
        "https://db.internal/",
        "https://printer.local/",
        "https://api.localhost/",
    ],
)
def test_rejects_internal_hosts(url: str) -> None:
    with pytest.raises(BadRequest, match="internal"):
        validate_long_url(url)


@pytest.mark.parametrize("url", ["", "   ", "https://", "not a url", "https:///path"])
def test_rejects_unusable_input(url: str) -> None:
    with pytest.raises(BadRequest):
        validate_long_url(url)


def test_rejects_over_length_url() -> None:
    long = "https://example.com/" + "a" * MAX_URL_LEN
    assert len(long) > MAX_URL_LEN
    with pytest.raises(BadRequest, match="too long"):
        validate_long_url(long)


def test_accepts_url_exactly_at_the_limit() -> None:
    """The boundary, so the rule is proven to reject *over*-length, not at-length."""
    at_limit = "https://example.com/" + "a" * (MAX_URL_LEN - len("https://example.com/"))
    assert len(at_limit) == MAX_URL_LEN
    assert validate_long_url(at_limit) == at_limit
