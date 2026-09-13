"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1–V4; those are yours, and each
vertical's "Proof" line names what it has to demonstrate. What is here is the
plumbing: the app boots, every route is reachable, the framework's validation
answers, the wired loops and the ffmpeg plumbing behave, and the unbuilt parts
raise.

That last group is the worklist made executable. When you implement a vertical,
its tests here are the first thing that should fail — delete them then.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from transcode_pipeline import dag, ffmpeg
from transcode_pipeline.chunk import ChunkPlan, plan_chunks
from transcode_pipeline.config import DEFAULT_LADDER, Settings
from transcode_pipeline.db import MIGRATIONS_DIR
from transcode_pipeline.errors import (
    AppError,
    BadRequestError,
    NotFoundError,
    TranscodeToolError,
    install_error_handlers,
)
from transcode_pipeline.main import build_state, create_app, stop_background
from transcode_pipeline.models import (
    TASK_KIND,
    JobContext,
    JobStatus,
    Rendition,
    Split,
    Stitch,
    Task,
    TaskStatus,
    Transcode,
)
from transcode_pipeline.state import AppState, task_failure
from transcode_pipeline.stitch import stitch
from transcode_pipeline.store import JobStore
from transcode_pipeline.worker import Worker

from .conftest import FfmpegTools

PROJECT_DIR = Path(__file__).resolve().parents[1]


def _task(kind: Split | Transcode | Stitch, status: TaskStatus = TaskStatus.RUNNING) -> Task:
    return Task(id=uuid4(), job_id=uuid4(), kind=kind, status=status)


def _unconnected_pool(settings: Settings) -> asyncpg.Pool[asyncpg.Record]:
    return asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=2)


# --------------------------------------------------------------------------- #
# The control-plane API
# --------------------------------------------------------------------------- #


async def test_healthz_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


async def test_metrics_render_in_prometheus_format(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


async def test_submit_reaches_v2(client: httpx.AsyncClient) -> None:
    response = await client.post("/jobs", json={"source": "bbb.mp4"})
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V2:")


async def test_submit_with_its_own_ladder_reaches_v2(client: httpx.AsyncClient) -> None:
    rung = {"name": "360p", "height": 360, "v_bitrate_kbps": 800, "a_bitrate_kbps": 96}
    response = await client.post("/jobs", json={"source": "bbb.mp4", "ladder": [rung]})
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V2:")


async def test_get_job_reaches_v2(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/jobs/{uuid4()}")
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V2:")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing_source"),
        pytest.param({"source": "bbb.mp4", "ladder": [{"name": "720p"}]}, id="incomplete_rung"),
        pytest.param(
            {
                "source": "bbb.mp4",
                "ladder": [
                    {"name": "x", "height": "tall", "v_bitrate_kbps": 1, "a_bitrate_kbps": 1}
                ],
            },
            id="non_numeric_height",
        ),
    ],
)
async def test_a_malformed_submit_is_a_422(client: httpx.AsyncClient, body: object) -> None:
    """Already true, courtesy of pydantic — before any vertical exists."""
    assert (await client.post("/jobs", json=body)).status_code == 422


async def test_a_non_json_submit_is_a_422(client: httpx.AsyncClient) -> None:
    assert (await client.post("/jobs", content=b"not json")).status_code == 422


async def test_a_job_id_that_is_not_a_uuid_is_a_422(client: httpx.AsyncClient) -> None:
    assert (await client.get("/jobs/not-a-uuid")).status_code == 422


async def test_the_job_view_is_a_published_schema(client: httpx.AsyncClient) -> None:
    """The polling contract is documented at /docs — Protocols → stable JSON."""
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]
    assert set(schemas["JobView"]["properties"]) == {
        "id",
        "source",
        "ladder",
        "status",
        "created_at",
        "tasks",
    }
    assert set(schemas["TaskCounts"]["properties"]) == {
        "pending",
        "ready",
        "running",
        "done",
        "failed",
    }


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "status"),
    [(NotFoundError, 404), (BadRequestError, 400), (TranscodeToolError, 500)],
)
def test_each_error_maps_to_its_status(error: type[AppError], status: int) -> None:
    assert error.status_code == status


async def test_a_5xx_never_shows_its_detail_and_a_4xx_does() -> None:
    app = FastAPI()
    install_error_handlers(app)

    async def boom() -> None:
        raise TranscodeToolError("`ffmpeg` exited 1: /srv/secret/source.mp4: Invalid data")

    async def missing() -> None:
        raise NotFoundError("job not found")

    app.add_api_route("/boom", boom)
    app.add_api_route("/missing", missing)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        boom_response = await http.get("/boom")
        missing_response = await http.get("/missing")

    assert boom_response.status_code == 500
    assert boom_response.json() == {"error": "internal server error"}
    assert missing_response.status_code == 404
    assert missing_response.json() == {"error": "job not found"}


# --------------------------------------------------------------------------- #
# Config — the type annotation is the parser
# --------------------------------------------------------------------------- #


def test_every_env_example_variable_is_a_settings_field() -> None:
    """One field per `.env.example` variable, and no orphan on either side."""
    text = (PROJECT_DIR / ".env.example").read_text(encoding="utf-8")
    keys = {m.group(1).lower() for m in re.finditer(r"^([A-Z_]+)=", text, re.MULTILINE)}
    assert keys == set(Settings.model_fields)


def test_defaults_match_the_compose_file() -> None:
    fields = Settings.model_fields
    assert fields["port"].default == 8080
    # Project-scoped host port: postgres 5432 → 54NN with NN = 12.
    assert ":5412/" in fields["database_url"].default
    assert "5412:5432" in (PROJECT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert fields["run_workers"].default is False


def test_intervals_convert_to_seconds_once() -> None:
    config = Settings(scheduler_interval_ms=250, poll_interval_ms=1500)
    assert config.scheduler_interval_secs == 0.25
    assert config.poll_interval_secs == 1.5


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"worker_concurrency": 0}, id="no_workers"),
        pytest.param({"lease_secs": 0}, id="zero_lease"),
        pytest.param({"target_chunk_secs": -1}, id="negative_target"),
        pytest.param({"port": 70000}, id="port_out_of_range"),
    ],
)
def test_a_nonsensical_setting_fails_at_startup(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(overrides)


def test_the_default_ladder_is_three_rungs() -> None:
    assert [rung.name for rung in DEFAULT_LADDER] == ["1080p", "720p", "480p"]
    assert Settings().default_ladder is DEFAULT_LADDER


# --------------------------------------------------------------------------- #
# Models — the Python side and the migration agree
# --------------------------------------------------------------------------- #


def _sql_enum(name: str) -> list[str]:
    sql = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
    match = re.search(rf"CREATE TYPE {name} AS ENUM \(([^)]*)\)", sql)
    assert match is not None, f"{name} not found in the migration"
    return re.findall(r"'([a-z_]+)'", match.group(1))


def test_task_status_matches_the_sql_enum() -> None:
    assert [s.value for s in TaskStatus] == _sql_enum("task_status")


def test_job_status_matches_the_sql_enum() -> None:
    assert [s.value for s in JobStatus] == _sql_enum("job_status")


@pytest.mark.parametrize(
    ("stored", "model"),
    [
        pytest.param({"op": "split"}, Split(), id="split"),
        pytest.param(
            {"op": "transcode", "chunk": 3, "rendition": "720p"},
            Transcode(chunk=3, rendition="720p"),
            id="transcode",
        ),
        pytest.param({"op": "stitch", "rendition": "720p"}, Stitch(rendition="720p"), id="stitch"),
    ],
)
def test_a_task_kind_is_exactly_the_jsonb_the_migration_documents(
    stored: dict[str, object], model: Split | Transcode | Stitch
) -> None:
    assert TASK_KIND.validate_python(stored) == model
    assert TASK_KIND.dump_python(model, mode="json") == stored


def test_an_unknown_task_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TASK_KIND.validate_python({"op": "reencode_everything"})


def test_a_task_round_trips_and_is_frozen() -> None:
    task = _task(Transcode(chunk=0, rendition="480p"), TaskStatus.PENDING)
    assert Task.model_validate_json(task.model_dump_json()) == task
    with pytest.raises(ValidationError):
        task.status = TaskStatus.DONE  # pyright: ignore[reportAttributeAccessIssue] - the point


def test_a_chunk_plan_knows_its_length() -> None:
    assert ChunkPlan(index=0, start=2.0, end=8.5).seconds == 6.5


# --------------------------------------------------------------------------- #
# The artifact layout
# --------------------------------------------------------------------------- #


def test_the_work_dir_layout(state: AppState) -> None:
    job = UUID("00000000-0000-0000-0000-000000000012")
    root = state.settings.work_dir
    assert state.workdir.job_dir(job) == root / "jobs" / str(job)
    assert state.workdir.chunk_dir(job, "720p") == root / "jobs" / str(job) / "720p" / "chunks"
    assert state.workdir.rendition_output(job, "720p") == root / "jobs" / str(job) / "720p/out.mp4"


# --------------------------------------------------------------------------- #
# ffmpeg plumbing — wired, so provable now
# --------------------------------------------------------------------------- #


async def test_a_missing_binary_is_a_tool_error() -> None:
    with pytest.raises(TranscodeToolError, match="spawn"):
        await ffmpeg.run("definitely-not-ffmpeg-12", ["-version"])


async def test_a_non_zero_exit_carries_the_tail_of_stderr() -> None:
    script = "import sys; sys.stderr.write('moov atom not found'); sys.exit(3)"
    with pytest.raises(TranscodeToolError, match="exited 3: moov atom not found"):
        await ffmpeg.run(sys.executable, ["-c", script])


async def test_arguments_are_never_parsed_by_a_shell(tmp_path: Path) -> None:
    """Security → no shell injection: a `;` in an argument is just a character."""
    out = tmp_path / "out.txt"
    hostile = 'bbb.mp4; touch "pwned"'
    script = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"
    await ffmpeg.run(sys.executable, ["-c", script, str(out), hostile])
    assert out.read_text() == hostile
    assert not (tmp_path / "pwned").exists()
    assert not Path("pwned").exists()


async def test_a_cancelled_command_kills_its_child(tmp_path: Path) -> None:
    """Shutdown's drain budget can cancel an encode; its ffmpeg must not outlive it."""
    pid_file = tmp_path / "pid"
    script = (
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    running = asyncio.create_task(ffmpeg.run(sys.executable, ["-c", script, str(pid_file)]))
    async with asyncio.timeout(10):
        while not (pid_file.exists() and pid_file.read_text()):
            await asyncio.sleep(0.02)
    pid = int(pid_file.read_text())

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _fake_tool(tmp_path: Path, name: str, stdout: str) -> str:
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({stdout!r})\n")
    path.chmod(0o755)
    return str(path)


async def test_keyframe_probe_output_is_parsed_tolerantly(tmp_path: Path) -> None:
    fake = _fake_tool(tmp_path, "ffprobe", "0.000000\n2.002000,\nN/A\n\n4.004000\n")
    assert await ffmpeg.probe_keyframes(fake, "bbb.mp4") == [0.0, 2.002, 4.004]


async def test_duration_probe_output_is_parsed(tmp_path: Path) -> None:
    assert await ffmpeg.probe_duration(_fake_tool(tmp_path, "ffprobe", "12.500\n"), "x") == 12.5


async def test_an_unparseable_duration_is_a_tool_error(tmp_path: Path) -> None:
    with pytest.raises(TranscodeToolError, match="could not parse duration"):
        await ffmpeg.probe_duration(_fake_tool(tmp_path, "ffprobe", "N/A\n"), "x")


async def test_probing_a_real_source(tmp_path: Path, ffmpeg_tools: FfmpegTools) -> None:
    """End to end against real ffmpeg: a 6 s clip with a keyframe every 2 s."""
    clip = tmp_path / "clip.mp4"
    await ffmpeg.run(
        ffmpeg_tools.ffmpeg,
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=24",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-g",
            "48",
            "-keyint_min",
            "48",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
    )
    keyframes = await ffmpeg.probe_keyframes(ffmpeg_tools.ffprobe, clip)
    duration = await ffmpeg.probe_duration(ffmpeg_tools.ffprobe, clip)
    assert keyframes == pytest.approx([0.0, 2.0, 4.0], abs=0.05)
    assert duration == pytest.approx(6.0, abs=0.1)


# --------------------------------------------------------------------------- #
# The wired loops: shutdown, settling, and a loud worklist
# --------------------------------------------------------------------------- #


class RecordingStore(JobStore):
    """A store whose settle methods record instead of reaching Postgres."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        super().__init__(pool)
        self.completed: list[UUID] = []
        self.failed: list[tuple[UUID, str, int]] = []

    async def job_context(self, job_id: UUID) -> JobContext:
        raise NotFoundError("job gone")

    async def complete(self, task_id: UUID) -> None:
        self.completed.append(task_id)

    async def fail(self, task_id: UUID, error: str, max_attempts: int) -> None:
        self.failed.append((task_id, error, max_attempts))


class NoopWorker(Worker):
    async def execute(self, task: Task) -> None:
        return None


async def test_a_finished_task_is_completed(state: AppState) -> None:
    store = RecordingStore(state.pool)
    task = _task(Split())
    await NoopWorker("w", store, state.workdir, state.settings).process(task)
    assert store.completed == [task.id]
    assert store.failed == []


async def test_a_failed_attempt_is_settled_with_fail(state: AppState) -> None:
    store = RecordingStore(state.pool)
    task = _task(Split())
    await Worker("w", store, state.workdir, state.settings).process(task)
    assert store.completed == []
    assert store.failed == [(task.id, "job gone", state.settings.max_attempts)]


async def test_an_unbuilt_vertical_ends_the_worker_instead_of_failing_the_task(
    state: AppState,
) -> None:
    store = RecordingStore(state.pool)
    with pytest.raises(NotImplementedError, match="V4"):
        await Worker("w", store, state.workdir, state.settings).process(
            _task(Stitch(rendition="720p"))
        )
    assert store.completed == []
    assert store.failed == []


async def test_a_worker_told_to_stop_never_claims(state: AppState) -> None:
    state.shutdown.set()
    worker = Worker("w", state.store, state.workdir, state.settings)
    # Would raise the V3 claim todo if it reached claim_ready.
    await asyncio.wait_for(worker.run(state.shutdown), timeout=1)


async def test_a_running_worker_surfaces_the_claim_todo(state: AppState) -> None:
    worker = Worker("w", state.store, state.workdir, state.settings)
    with pytest.raises(NotImplementedError, match="V3"):
        await asyncio.wait_for(worker.run(state.shutdown), timeout=1)


async def test_the_scheduler_stops_on_shutdown(state: AppState) -> None:
    state.shutdown.set()
    await asyncio.wait_for(dag.schedule_loop(state.store, 0.01, state.shutdown), timeout=1)


async def test_the_scheduler_surfaces_the_readiness_todo(state: AppState) -> None:
    with pytest.raises(NotImplementedError, match="V2"):
        await asyncio.wait_for(dag.schedule_loop(state.store, 0.01, state.shutdown), timeout=1)


async def test_one_failed_scheduler_tick_does_not_stop_scheduling(state: AppState) -> None:
    shutdown = state.shutdown

    class FlakyStore(JobStore):
        promotions = 0
        reclaims = 0

        async def promote_ready(self) -> int:
            self.promotions += 1
            if self.promotions == 1:
                raise OSError("postgres blipped")
            shutdown.set()
            return 0

        async def reclaim_expired(self) -> int:
            self.reclaims += 1
            return 0

    store = FlakyStore(state.pool)
    await asyncio.wait_for(dag.schedule_loop(store, 0.01, shutdown), timeout=1)
    assert store.promotions == 2
    assert store.reclaims == 2


async def test_run_workers_starts_a_pool_that_stops_at_the_worklist(settings: Settings) -> None:
    config = Settings(
        run_workers=True,
        worker_concurrency=2,
        work_dir=settings.work_dir,
        scheduler_interval_ms=10,
        poll_interval_ms=10,
    )
    state = build_state(config, pool=_unconnected_pool(config))
    app = create_app(state=state)
    async with app.router.lifespan_context(app):
        assert [t.get_name() for t in state.background][0] == "scheduler"
        assert len(state.background) == 1 + config.worker_concurrency
        await asyncio.wait(state.background, timeout=2)
        failures = [task_failure(t) for t in state.background]
        assert all(isinstance(exc, NotImplementedError) for exc in failures)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            assert (await http.get("/healthz")).status_code == 200


async def test_shutdown_waits_for_a_task_that_settles_in_time(state: AppState) -> None:
    async def polite() -> None:
        await state.shutdown.wait()

    task = asyncio.create_task(polite())
    state.background.append(task)
    await stop_background(state, drain_secs=1)
    assert task.done()
    assert not task.cancelled()


async def test_shutdown_cancels_what_outlives_the_drain_budget(state: AppState) -> None:
    stuck = asyncio.create_task(asyncio.sleep(30))
    state.background.append(stuck)
    await stop_background(state, drain_secs=0.05)
    assert stuck.cancelled()


# --------------------------------------------------------------------------- #
# The worklist, made executable. Delete each as you implement its vertical.
# --------------------------------------------------------------------------- #


def test_v1_chunk_planner_is_unwritten() -> None:
    with pytest.raises(NotImplementedError, match="V1"):
        plan_chunks([0.0, 2.0, 4.0], 6.0, 6.0)


def test_v2_dag_is_unwritten() -> None:
    chunks = [ChunkPlan(index=0, start=0.0, end=6.0)]
    with pytest.raises(NotImplementedError, match="V2"):
        dag.expand(uuid4(), uuid4(), chunks, DEFAULT_LADDER)
    with pytest.raises(NotImplementedError, match="V2"):
        dag.newly_ready([_task(Split(), TaskStatus.READY)])


def test_deps_all_done_is_the_wired_spec_of_runnable() -> None:
    dep = uuid4()
    pending = Task(
        id=uuid4(),
        job_id=uuid4(),
        kind=Stitch(rendition="720p"),
        status=TaskStatus.PENDING,
        deps=(dep,),
    )
    assert dag.deps_all_done(pending, lambda d: d == dep)
    assert not dag.deps_all_done(pending, lambda _d: False)
    running = pending.model_copy(update={"status": TaskStatus.RUNNING})
    assert not dag.deps_all_done(running, lambda _d: True)


async def test_v2_store_is_unwritten(state: AppState) -> None:
    store = state.store
    rung = Rendition(name="720p", height=720, v_bitrate_kbps=2800, a_bitrate_kbps=128)
    for call in (
        store.submit("bbb.mp4", [rung]),
        store.get_job(uuid4()),
        store.job_context(uuid4()),
        store.add_tasks([]),
        store.promote_ready(),
    ):
        with pytest.raises(NotImplementedError, match="V2"):
            await call


async def test_v3_lease_is_unwritten(state: AppState) -> None:
    store = state.store
    for call in (
        store.claim_ready("w", 120.0),
        store.complete(uuid4()),
        store.fail(uuid4(), "boom", 3),
        store.reclaim_expired(),
    ):
        with pytest.raises(NotImplementedError, match="V3"):
            await call


async def test_v3_transcode_is_unwritten(state: AppState) -> None:
    worker = Worker("w", state.store, state.workdir, state.settings)
    task = _task(Transcode(chunk=0, rendition="720p"))
    with pytest.raises(NotImplementedError, match="V3"):
        await worker.transcode_chunk(task, 0, "720p")


async def test_v4_stitch_is_unwritten(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError, match="V4"):
        await stitch("ffmpeg", tmp_path / "chunks", tmp_path / "out.mp4")


def test_the_traversal_guard_is_unwritten(state: AppState) -> None:
    with pytest.raises(NotImplementedError, match="security"):
        state.workdir.resolve_source("../../etc/passwd")


# --------------------------------------------------------------------------- #
# Needs Postgres — skipped without it
# --------------------------------------------------------------------------- #


async def test_the_migration_creates_the_dag_schema(
    pg_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    for table in ("jobs", "tasks", "task_deps"):
        assert await pg_pool.fetchval("SELECT to_regclass($1)::text", table) == table
