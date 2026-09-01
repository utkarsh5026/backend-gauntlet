"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V7; those are yours to
write, and the SPEC's "Proof" lines say what each one has to demonstrate. What
is here is the plumbing: the app boots, the engine opens over an empty
directory, both listeners answer, config parses and does not leak the password,
the command table dispatches, and the unbuilt parts raise.

That last group is the worklist made executable. When you implement a vertical,
those tests are the first thing that should fail — delete them then. They exist
so that "the scaffold is in its expected state" is something the test suite
asserts rather than something you assume.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError

from lsm_redis import metrics
from lsm_redis.block_cache import BlockCache, BlockKey
from lsm_redis.bloom import Bloom
from lsm_redis.compaction import compaction_loop
from lsm_redis.config import Settings, SyncPolicy
from lsm_redis.engine import Engine
from lsm_redis.errors import Corrupt, NoAuth, ProtocolError, StorageError, WrongPass
from lsm_redis.memtable import TOMBSTONE, Entry, Memtable
from lsm_redis.resp import Error
from lsm_redis.server import Connection, RespServer, dispatch
from lsm_redis.wal import Op, Wal, WalRecord
from tests.conftest import RespClient

# --- the HTTP sidecar ---------------------------------------------------------


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_stats_on_a_fresh_engine(client: httpx.AsyncClient) -> None:
    """Every counter starts at zero on an empty data dir — which is also the
    proof that `Engine.open` does not trip a vertical on a fresh directory."""
    response = await client.get("/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["keys_memtable"] == 0
    assert body["memtable_bytes"] == 0
    assert body["sstables"] == 0
    assert body["sequence"] == 0
    assert body["block_cache_capacity_bytes"] == 64 * 1024


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


# --- security: the password never leaves the process --------------------------


async def test_config_endpoint_does_not_leak_the_password(
    settings: Settings, client: httpx.AsyncClient
) -> None:
    """The SPEC's security item, as a test rather than a promise.

    `public_stats` is an allowlist, so this passes today *and* keeps passing when
    someone adds a second secret field — which is the whole reason it is an
    allowlist. Note that the fixture has no password set, so the assertion is
    about the *shape* of the response, not about one string being absent.
    """
    response = await client.get("/config")
    assert response.status_code == 200
    body = response.json()
    assert "requirepass" not in body
    assert body["auth_required"] is False
    assert set(body) == set(settings.public_stats())


def test_password_is_absent_from_public_stats_even_when_set() -> None:
    published = Settings(requirepass="hunter2").public_stats()
    assert "hunter2" not in str(published)
    assert published["auth_required"] is True


# --- config -------------------------------------------------------------------


def test_milliseconds_become_seconds() -> None:
    """The environment speaks ms because that is readable; asyncio speaks
    seconds. The conversion happens once, in `Settings`."""
    assert Settings(compaction_interval_ms=2500).compaction_interval == 2.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("always", SyncPolicy.ALWAYS),
        ("everysec", SyncPolicy.EVERYSEC),
        ("no", SyncPolicy.NO),
    ],
)
def test_wal_sync_policy_parses(raw: str, expected: SyncPolicy) -> None:
    assert Settings(wal_sync=SyncPolicy(raw)).wal_sync is expected


def test_an_unknown_sync_policy_fails_at_startup() -> None:
    """Not silently defaulted. `WAL_SYNC=alwayss` must stop the process, because
    the alternative is a store that quietly runs with weaker durability than the
    operator asked for and never says so."""
    with pytest.raises(ValidationError):
        Settings(wal_sync="sometimes")  # type: ignore[arg-type]  # the point of the test


def test_block_cache_may_be_disabled_but_ports_may_not_be_negative() -> None:
    assert Settings(block_cache_bytes=0).block_cache_bytes == 0
    with pytest.raises(ValidationError):
        Settings(http_port=0)


# --- errors -------------------------------------------------------------------


def test_client_errors_keep_their_message_on_both_wires() -> None:
    """A client that cannot tell "wrong password" from "malformed frame" cannot
    fix either, so these keep their redis error words."""
    assert NoAuth().resp_error().startswith("NOAUTH ")
    assert WrongPass().resp_error().startswith("WRONGPASS ")
    assert ProtocolError("bad bulk length").resp_error() == "ERR bad bulk length"
    assert ProtocolError().status_code == 400


def test_server_faults_never_describe_the_disk() -> None:
    """`Corrupt` knows which block of which file failed its CRC. A client learns
    none of that — it is a map of your storage, and it belongs in the log."""
    for error in (Corrupt(), StorageError()):
        assert error.resp_error() == "ERR internal error"
        assert error.status_code == 500


# --- the engine's wired half --------------------------------------------------


def test_open_creates_the_data_directory(settings: Settings) -> None:
    assert not settings.data_dir.exists()
    engine = Engine.open(settings)
    assert settings.data_dir.is_dir()
    assert engine.stats().sstables == 0


def test_reopening_an_existing_store_is_fine(settings: Settings) -> None:
    """Recovery over an empty WAL must not reach V2 — otherwise the scaffold
    could not start twice, and neither could a server that had never been
    written to."""
    first = Engine.open(settings)
    asyncio.run(first.close())
    second = Engine.open(settings)
    assert second.stats().keys_memtable == 0


def test_sequence_numbers_are_monotonic(engine: Engine) -> None:
    assert [engine.next_seq() for _ in range(3)] == [1, 2, 3]
    assert engine.stats().sequence == 3


def test_sstable_ids_are_unique(engine: Engine) -> None:
    assert len({engine.next_sstable_id() for _ in range(5)}) == 5


def test_unreadable_files_in_the_data_dir_are_ignored(settings: Settings) -> None:
    """A stray file must not stop the store from opening. The data directory is
    shared with the WAL, with temp files from an interrupted flush, and with
    whatever an operator left there."""
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "notes.txt").write_text("hi")
    (settings.data_dir / "not-a-number.sst").write_bytes(b"")
    assert Engine.open(settings).stats().sstables == 0


# --- the RESP listener --------------------------------------------------------


async def test_resp_port_accepts_connections(resp_server: RespServer) -> None:
    """The accept loop is real before the codec is: a client can connect and the
    connection stays open. That is what makes the *next* test's failure
    informative — it isolates "V1 is unwritten" from "nothing is listening"."""
    assert resp_server.port > 0
    with socket.create_connection(("127.0.0.1", resp_server.port), timeout=2) as sock:
        assert sock.fileno() > 0


async def test_a_connection_that_says_nothing_stays_open(resp: RespClient) -> None:
    """`redis-cli` connects and then waits for you to type. A server that parses
    an empty buffer on connect would drop it before the first command."""
    assert await resp.read(timeout=0.2) == b""


async def test_the_first_command_trips_v1(resp: RespClient) -> None:
    """The worklist, made executable.

    A stock client's `PING` reaches `resp.parse_command`, which raises, which
    ends that connection's task — while the server keeps serving. Delete this
    test the moment V1 exists; it should be the first thing that fails.
    """
    await resp.send(b"*1\r\n$4\r\nPING\r\n")
    assert await resp.read() == b"", "V1 is implemented — delete this test and assert +PONG"


async def test_the_server_survives_a_connection_that_died(
    resp_server: RespServer, client: httpx.AsyncClient
) -> None:
    """One connection tripping an unbuilt vertical must not take the process
    down. That is the property that makes the scaffold usable at all."""
    reader, writer = await asyncio.open_connection("127.0.0.1", resp_server.port)
    writer.write(b"*1\r\n$4\r\nPING\r\n")
    await writer.drain()
    await reader.read(1024)
    writer.close()

    assert (await client.get("/healthz")).status_code == 200
    reader2, writer2 = await asyncio.open_connection("127.0.0.1", resp_server.port)
    writer2.close()
    assert reader2 is not None


# --- the command table, which does not need the codec -------------------------


async def _reply(engine: Engine, settings: Settings, conn: Connection, *args: bytes) -> object:
    return await dispatch(engine, settings, conn, list(args))


async def test_ping_and_echo_answer_without_touching_the_engine(
    engine: Engine, settings: Settings
) -> None:
    """`dispatch` is a plain function over parsed commands, so the command table
    is testable before the codec that would feed it exists."""
    conn = Connection("test", authenticated=True)
    assert await _reply(engine, settings, conn, b"PING") == "PONG"
    assert await _reply(engine, settings, conn, b"PING", b"hi") == b"hi"
    assert await _reply(engine, settings, conn, b"ECHO", b"hi") == b"hi"
    assert await _reply(engine, settings, conn, b"COMMAND") == []
    assert await _reply(engine, settings, conn, b"QUIT") == "OK"


async def test_command_names_are_case_insensitive(engine: Engine, settings: Settings) -> None:
    conn = Connection("test", authenticated=True)
    assert await _reply(engine, settings, conn, b"ping") == "PONG"


async def test_unknown_and_wrong_arity_commands_are_errors_not_disconnects(
    engine: Engine, settings: Settings
) -> None:
    """The horizontal item: a protocol error is not a connection error. The
    reply is an `Error` value, and `dispatch` returns rather than raising, so the
    connection loop writes it and keeps reading."""
    conn = Connection("test", authenticated=True)
    unknown = await _reply(engine, settings, conn, b"FLUSHALL")
    assert isinstance(unknown, Error)
    assert "unknown command" in unknown.message

    arity = await _reply(engine, settings, conn, b"ECHO")
    assert isinstance(arity, Error)
    assert "wrong number of arguments" in arity.message


async def test_auth_gate(engine: Engine) -> None:
    """The whole security vertical that does not need a single vertical built.

    A connection with a password configured starts unauthenticated: everything
    but the handshake commands is `NOAUTH`, a wrong password is `WRONGPASS`, and
    only the right one opens the connection.
    """
    settings = Settings(requirepass="hunter2")
    conn = Connection("test", authenticated=False)

    blocked = await _reply(engine, settings, conn, b"GET", b"k")
    assert isinstance(blocked, Error)
    assert blocked.message.startswith("NOAUTH")

    # PING is gated too — being able to probe a password-protected server for
    # liveness without the password is a decision, and redis's is "no".
    assert isinstance(await _reply(engine, settings, conn, b"PING"), Error)

    wrong = await _reply(engine, settings, conn, b"AUTH", b"letmein")
    assert isinstance(wrong, Error)
    assert wrong.message.startswith("WRONGPASS")
    assert not conn.authenticated

    assert await _reply(engine, settings, conn, b"AUTH", b"hunter2") == "OK"
    assert conn.authenticated
    assert await _reply(engine, settings, conn, b"PING") == "PONG"


async def test_auth_on_an_open_server_is_an_error(engine: Engine, settings: Settings) -> None:
    """Matching real redis: `AUTH` when no password is set is a client mistake
    worth reporting, not a silent success."""
    conn = Connection("test", authenticated=True)
    reply = await _reply(engine, settings, conn, b"AUTH", b"anything")
    assert isinstance(reply, Error)
    assert "no password is set" in reply.message


# --- metrics ------------------------------------------------------------------


def test_latency_buckets_bracket_the_p99_target() -> None:
    """A histogram whose buckets do not bracket the SLO is a metric that cannot
    fail. The SPEC's target is p99 <= 10 ms, so 0.01 has to be a bucket edge —
    `prometheus_client`'s defaults jump straight from 0.005 to 0.01 to 0.025 with
    nothing useful below, which would let a 9 ms p99 and a 1 ms p99 look the
    same."""
    assert 0.01 in metrics.COMMAND_BUCKETS
    assert min(metrics.COMMAND_BUCKETS) <= 0.0001
    assert 0.001 in metrics.FSYNC_BUCKETS


def test_cache_lookups_are_two_counters_not_a_ratio() -> None:
    """Counters per outcome, never a pre-computed ratio: a ratio cannot be
    aggregated across replicas or re-windowed after the fact, and two counters
    can. `outcome` is therefore the only label, and a `..._ratio` series must not
    exist for anything to divide the wrong way."""
    metrics.BLOCK_CACHE_LOOKUPS_TOTAL.labels(outcome="hit")
    metrics.BLOCK_CACHE_LOOKUPS_TOTAL.labels(outcome="miss")
    with pytest.raises(ValueError):
        metrics.BLOCK_CACHE_LOOKUPS_TOTAL.labels(outcome="hit", shard="0")

    exported = {metric.name for metric in REGISTRY.collect()}
    assert "lsm_block_cache_lookups" in exported
    assert not [name for name in exported if name.endswith("_ratio")]


# --- the compaction loop ------------------------------------------------------


async def test_compaction_loop_survives_the_unbuilt_vertical(engine: Engine) -> None:
    """The loop must tolerate `run_compaction` raising: on the scaffold it does,
    every tick, and a compactor that died on the first tick would look exactly
    like one that was working."""
    task = asyncio.create_task(compaction_loop(engine, interval=0.01))
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_compaction_loop_stops_on_cancel(engine: Engine) -> None:
    """`CancelledError` inherits from `BaseException`, so the loop's
    `except Exception` does not swallow it. If it did, shutdown would hang."""
    task = asyncio.create_task(compaction_loop(engine, interval=60))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- graceful shutdown --------------------------------------------------------


async def test_lifespan_stops_accepting_and_closes_the_wal(settings: Settings) -> None:
    """The graceful-shutdown criterion's wired half: leaving the lifespan closes
    the listener and syncs the log. The other half — that nothing acknowledged is
    lost across `kill -9` — is V2's, and lives in the crash test you write."""
    from lsm_redis.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        server = app.state.resp_server
        assert isinstance(server, RespServer)
        port = server.port
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()

    wal_path = settings.data_dir / "wal.log"
    assert wal_path.exists()


# --- the worklist: everything below raises until you build it -----------------


def test_wal_append_and_replay_are_unbuilt(settings: Settings, tmp_path: Path) -> None:
    wal = Wal.open(tmp_path / "wal.log", settings.wal_sync)
    record = WalRecord(seq=1, op=Op.SET, key=b"k", value=b"v")
    with pytest.raises(NotImplementedError):
        wal.append(record)
    with pytest.raises(NotImplementedError):
        list(Wal.replay(tmp_path / "wal.log"))


def test_memtable_is_unbuilt() -> None:
    table = Memtable()
    with pytest.raises(NotImplementedError):
        table.insert(b"k", b"v", 1)
    with pytest.raises(NotImplementedError):
        table.get(b"k")
    with pytest.raises(NotImplementedError):
        list(table.items_sorted())


def test_bloom_is_unbuilt() -> None:
    with pytest.raises(NotImplementedError):
        Bloom.sized(100, 10)
    loaded = Bloom.from_parts(b"\x00" * 8, k=3)
    with pytest.raises(NotImplementedError):
        loaded.insert(b"k")
    with pytest.raises(NotImplementedError):
        loaded.maybe_contains(b"k")


def test_block_cache_is_unbuilt() -> None:
    cache = BlockCache(1024)
    with pytest.raises(NotImplementedError):
        cache.get(BlockKey(1, 0))
    with pytest.raises(NotImplementedError):
        cache.insert(BlockKey(1, 0), b"block")


async def test_engine_paths_are_unbuilt(engine: Engine) -> None:
    with pytest.raises(NotImplementedError):
        await engine.get(b"k")
    with pytest.raises(NotImplementedError):
        await engine.set(b"k", b"v")
    with pytest.raises(NotImplementedError):
        await engine.delete(b"k")
    with pytest.raises(NotImplementedError):
        await engine.flush_memtable()
    with pytest.raises(NotImplementedError):
        await engine.run_compaction()


# --- the pieces that are wired, and stay wired --------------------------------


def test_tombstone_is_a_distinct_answer_from_absence() -> None:
    """The single most important type distinction in the project: `None` means
    "this level has no opinion", `TOMBSTONE` means "deleted here, stop looking".
    Collapse them and deleted keys come back from disk."""
    assert TOMBSTONE is not None
    assert Entry(seq=1, value=TOMBSTONE).is_tombstone
    assert not Entry(seq=1, value=b"v").is_tombstone


def test_empty_memtable_reports_empty() -> None:
    table = Memtable()
    assert len(table) == 0
    assert table.approx_bytes == 0
    assert not table.is_full(1)


def test_disabled_block_cache_reports_a_zero_budget() -> None:
    cache = BlockCache(0)
    assert cache.capacity_bytes == 0
    assert cache.used_bytes == 0
    assert cache.stats() == (0, 0)
