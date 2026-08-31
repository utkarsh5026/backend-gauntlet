"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V4. They assert the plumbing
(the app boots, config parses the cluster, the state directory is laid out,
validation runs at the edge, `/status` and `/metrics` render) and they pin the
scaffold's contract: the consensus paths raise until you build them.

The last three tests are the worklist made executable. When you implement V1, V2
and V3, they are the first things that should fail — delete them then.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from raft_kv.config import Settings, parse_peers, port_of
from raft_kv.log import RaftLog
from raft_kv.node import Role
from raft_kv.rpc import LogEntry, NoopCommand, SetCommand
from raft_kv.store import Store

# --- wiring -------------------------------------------------------------------


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    """An inbound id survives the hop — it is what correlates one client write
    with the AppendEntries every other node logged for it."""
    response = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


async def test_status_reports_a_fresh_follower(client: httpx.AsyncClient) -> None:
    """Every node starts as a follower at term 0 with nothing committed — exactly
    the state a just-booted or just-crashed node is in before it hears from
    anyone."""
    body = await client.get("/status")
    assert body.json() == {
        "id": 1,
        "role": Role.FOLLOWER,
        "term": 0,
        "leader_id": None,
        "commit_index": 0,
        "last_applied": 0,
        "log_last_index": 0,
        "cluster_size": 1,
        "match_index": None,
    }


def test_state_dir_is_created_on_open(tmp_path: Path) -> None:
    """Recovery has to have somewhere to look, so the directory exists before any
    request does."""
    settings = Settings(node_id=2, peers="1=h:1,2=h:2", data_dir=tmp_path / "data")
    RaftLog.open(settings.state_path)
    assert settings.state_path.parent.is_dir()
    assert settings.state_path.parent.name == "node-2"


# --- config: PEERS is the cluster --------------------------------------------


def test_peers_parses_into_a_cluster() -> None:
    assert parse_peers("1=127.0.0.1:9001, 2=127.0.0.1:9002") == {
        1: "127.0.0.1:9001",
        2: "127.0.0.1:9002",
    }


@pytest.mark.parametrize("raw", ["", "1", "=1.2.3.4:1", "x=1.2.3.4:1", "1="])
def test_malformed_peers_fails_at_startup(raw: str) -> None:
    """A silently-dropped peer changes `quorum()`, so this fails the process
    rather than booting a node with the wrong idea of its own cluster."""
    with pytest.raises(ValidationError):
        Settings(peers=raw)


def test_bind_port_and_peer_set_come_from_peers() -> None:
    """One list defines the whole topology: what this node binds, and who it
    talks to."""
    settings = Settings(node_id=2, peers="1=127.0.0.1:9001,2=127.0.0.1:9002")
    assert settings.bind_port == 9002
    assert settings.peer_addrs == {1: "127.0.0.1:9001"}
    assert 2 not in settings.peer_addrs


def test_node_id_absent_from_peers_is_an_error() -> None:
    with pytest.raises(ValueError, match="not found in PEERS"):
        _ = Settings(node_id=7, peers="1=127.0.0.1:9001").self_addr


def test_port_of_rejects_an_address_without_one() -> None:
    with pytest.raises(ValueError, match="no port"):
        port_of("127.0.0.1")


# --- log geometry: the index math the consensus code leans on ----------------


def _entry(index: int, term: int = 1) -> LogEntry:
    return LogEntry(term=term, index=index, command=SetCommand(key=f"k{index}", value="v"))


def test_empty_log_geometry(tmp_path: Path) -> None:
    log = RaftLog.open(tmp_path / "state.json")
    assert log.last_index == 0
    assert log.last_term == 0
    assert log.get(1) is None
    assert log.entries_from(1) == []


def test_append_truncate_and_lookup(tmp_path: Path) -> None:
    log = RaftLog.open(tmp_path / "state.json")
    log.append([_entry(1), _entry(2), _entry(3)])
    assert log.last_index == 3
    assert log.term_at(2) == 1
    assert [e.index for e in log.entries_from(2)] == [2, 3]

    log.truncate_from(2)
    assert log.last_index == 1
    assert log.get(2) is None


def test_lookups_below_the_snapshot_base_return_none(tmp_path: Path) -> None:
    """The negative-index trap, pinned.

    After compaction the position of a compacted index is negative, and a bare
    `entries[pos]` would hand back an entry from the *end* of the log — plausible
    data that the consistency check would happily match against. It must be
    `None`, and a slice from below the base must not silently become a tail.
    """
    log = RaftLog.open(tmp_path / "state.json")
    log.append([_entry(1), _entry(2), _entry(3), _entry(4)])
    log.compact_to(3, 1)

    assert len(log) == 1
    assert log.get(1) is None
    assert log.get(2) is None
    assert log.get(4) is not None
    assert [e.index for e in log.entries_from(1)] == [4]
    # The seam itself still answers, which is what keeps the consistency check
    # working across it.
    assert log.term_at(3) == 1
    assert log.snapshot_point == (3, 1)


# --- the scaffold's worklist, pinned -----------------------------------------


def test_store_apply_is_still_a_todo() -> None:
    """Delete once V3 lands."""
    store = Store()
    assert store.last_applied == 0
    with pytest.raises(NotImplementedError):
        store.apply(LogEntry(term=1, index=1, command=NoopCommand()))


async def test_write_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """Delete once V2 lands."""
    with pytest.raises(NotImplementedError):
        await client.put("/kv/a", json={"value": "hello"})


async def test_read_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """Delete once V2/V3's read path lands."""
    with pytest.raises(NotImplementedError):
        await client.get("/kv/a")


async def test_request_vote_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """Delete once V1 lands."""
    with pytest.raises(NotImplementedError):
        await client.post(
            "/raft/request-vote",
            json={"term": 1, "candidate_id": 2, "last_log_index": 0, "last_log_term": 0},
        )


# --- input validation happens before anything reaches the log ----------------


async def test_oversized_key_is_rejected_before_the_log(client: httpx.AsyncClient) -> None:
    """A log entry is permanent — it is replicated to every node and lands in
    every future snapshot. The cap is enforced at the edge, before `propose`."""
    response = await client.put(f"/kv/{'k' * 2000}", json={"value": "v"})
    assert response.status_code == 400


async def test_oversized_value_is_rejected_before_the_log(client: httpx.AsyncClient) -> None:
    response = await client.put("/kv/a", json={"value": "v" * 300_000})
    assert response.status_code == 422
