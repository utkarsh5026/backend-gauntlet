"""`NewJob`'s caps — the SPEC's "validate and **cap** everything the caller controls".

These are the boundary of the system: everything below assumes a job has already
been through here, so every bound worth having is worth a test that it is
inclusive on the right side.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from job_queue.job import (
    MAX_ATTEMPTS_CEILING,
    MAX_DELAY_SECS,
    MAX_NAME_LEN,
    MAX_PAYLOAD_BYTES,
    NewJob,
)


def valid(**overrides: object) -> NewJob:
    """A well-formed job the negative cases mutate one field at a time."""
    fields: dict[str, object] = {
        "queue": "emails",
        "kind": "send_email",
        "payload": {"to": "a@b.com"},
        "max_attempts": 5,
        "delay_secs": 60,
    }
    fields.update(overrides)
    return NewJob(**fields)  # type: ignore[arg-type]  # kwargs are checked by pydantic


def test_accepts_a_well_formed_job() -> None:
    assert valid().kind == "send_email"


def test_accepts_boundary_values() -> None:
    """Every cap is *inclusive* — the documented maximum must itself be accepted."""
    job = valid(
        max_attempts=MAX_ATTEMPTS_CEILING,
        delay_secs=MAX_DELAY_SECS,
        queue="a" * MAX_NAME_LEN,
    )
    assert job.max_attempts == MAX_ATTEMPTS_CEILING
    assert job.delay_secs == MAX_DELAY_SECS


def test_rejects_empty_or_overlong_names() -> None:
    with pytest.raises(ValidationError):
        valid(queue="")
    with pytest.raises(ValidationError):
        valid(kind="k" * (MAX_NAME_LEN + 1))


@pytest.mark.parametrize("bad", ["my queue", "emails!", "a/b", "drop;table", "café"])
def test_rejects_bad_charset_in_names(bad: str) -> None:
    """A queue name becomes a NOTIFY channel and a metric label, so the charset is
    not cosmetic: arbitrary values would blow up label cardinality and produce
    surprising channel names."""
    with pytest.raises(ValidationError):
        valid(queue=bad)


def test_rejects_oversized_payload() -> None:
    with pytest.raises(ValidationError):
        valid(payload={"blob": "x" * (MAX_PAYLOAD_BYTES + 1)})


@pytest.mark.parametrize("bad", [0, -1, MAX_ATTEMPTS_CEILING + 1])
def test_rejects_out_of_range_max_attempts(bad: int) -> None:
    with pytest.raises(ValidationError):
        valid(max_attempts=bad)


def test_rejects_delay_over_the_ceiling() -> None:
    with pytest.raises(ValidationError):
        valid(delay_secs=MAX_DELAY_SECS + 1)


def test_queue_defaults_and_payload_defaults_to_empty_object() -> None:
    """A body carrying only `kind` is valid — the queue and payload have defaults."""
    job = NewJob(kind="noop")
    assert job.queue == "default"
    assert job.payload == {}
