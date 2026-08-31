"""Decoding and dispatch — the app layer the queue core knows nothing about.

Almost all of this is database-free: a `Job` is just a value here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from job_queue.handlers import (
    MAX_LOG_LINES,
    Exec,
    Fail,
    JobFailed,
    Noop,
    Sleep,
    decode,
    dispatch,
    job_log_path,
)
from job_queue.job import Job, JobState


def job(kind: str, payload: Any = None, *, attempts: int = 0, job_id: int = 1) -> Job:
    """A minimal `Job` for exercising decode/dispatch without a database."""
    now = datetime.now(UTC)
    return Job(
        id=job_id,
        queue="default",
        kind=kind,
        payload=payload,
        state=JobState.RUNNING,
        attempts=attempts,
        max_attempts=5,
        run_at=now,
        locked_until=None,
        last_error=None,
        created_at=now,
    )


# ---- decoding --------------------------------------------------------------


def test_decodes_unit_variant_from_null_payload() -> None:
    assert isinstance(decode(job("noop", None)), Noop)


def test_decodes_data_variant() -> None:
    decoded = decode(job("sleep", {"ms": 100}))
    assert isinstance(decoded, Sleep)
    assert decoded.payload.ms == 100


def test_decodes_exec_with_defaulted_fields() -> None:
    """`args` and `timeout_secs` are optional — a bare `program` is a valid exec job."""
    decoded = decode(job("exec", {"program": "true"}))
    assert isinstance(decoded, Exec)
    assert decoded.payload.args == []
    assert decoded.payload.timeout_secs is None


def test_rejects_unknown_kind() -> None:
    """An unrecognised kind is a bad enqueue, not something to run — and it fails as
    a *job*, so it lands in `last_error` and the DLQ rather than killing a worker."""
    with pytest.raises(JobFailed):
        decode(job("definitely_not_a_kind", {}))


def test_rejects_malformed_payload() -> None:
    with pytest.raises(JobFailed):
        decode(job("sleep", {"ms": "not-a-number"}))


# ---- dispatch --------------------------------------------------------------


async def test_dispatch_noop_ok_and_fail_err(tmp_path: Path) -> None:
    await dispatch(job("noop"), tmp_path)  # must not raise
    with pytest.raises(JobFailed):
        await dispatch(job("fail"), tmp_path)


async def test_fail_is_a_poison_message() -> None:
    """`fail` is the fixture V3 is built around: it never succeeds, at any attempt."""
    decoded = decode(job("fail"))
    assert isinstance(decoded, Fail)


async def test_flaky_fails_until_attempts_exceed_fail_n(tmp_path: Path) -> None:
    payload = {"fail_n": 2}
    for attempt in (0, 1, 2):
        with pytest.raises(JobFailed):
            await dispatch(job("flaky_then_ok", payload, attempts=attempt), tmp_path)
    await dispatch(job("flaky_then_ok", payload, attempts=3), tmp_path)


# ---- process execution -----------------------------------------------------


async def test_writes_both_streams_to_the_log_file(tmp_path: Path) -> None:
    target = job("shell", {"script": "echo hello; echo oops >&2"})
    await dispatch(target, tmp_path)

    contents = job_log_path(tmp_path, target).read_text()
    assert "[out] hello" in contents
    assert "[err] oops" in contents


async def test_nonzero_exit_reports_code_and_stderr_tail(tmp_path: Path) -> None:
    target = job("shell", {"script": "echo boom >&2; exit 3"})
    with pytest.raises(JobFailed) as caught:
        await dispatch(target, tmp_path)

    message = str(caught.value)
    assert "exit 3" in message
    assert "boom" in message, "the stderr tail is what makes last_error actionable"


async def test_hung_command_times_out(tmp_path: Path) -> None:
    """A command that outruns its timeout is killed.

    This is a V2 concern, not a cosmetic one: a process still running when its
    lease expires gets a *second, concurrent* copy started by the reaper.
    """
    target = job("shell", {"script": "sleep 30", "timeout_secs": 0.5})
    with pytest.raises(JobFailed) as caught:
        await dispatch(target, tmp_path)
    assert "timed out" in str(caught.value)


async def test_output_is_capped_with_a_truncation_marker(tmp_path: Path) -> None:
    """Past the cap the file stops growing — but the pipe keeps draining, or the
    child would block on a full buffer and never exit."""
    target = job("shell", {"script": f"seq 1 {MAX_LOG_LINES + 200}"})
    await dispatch(target, tmp_path)

    lines = job_log_path(tmp_path, target).read_text().splitlines()
    assert len(lines) == MAX_LOG_LINES + 1, "capped lines plus one truncation marker"
    assert "truncated" in lines[-1]


def test_log_path_is_keyed_by_id_and_attempt(tmp_path: Path) -> None:
    """A retry must not overwrite the evidence from the attempt that failed."""
    first = job_log_path(tmp_path, job("noop", attempts=0, job_id=42))
    second = job_log_path(tmp_path, job("noop", attempts=1, job_id=42))
    assert first == tmp_path / "42" / "0.log"
    assert second == tmp_path / "42" / "1.log"
