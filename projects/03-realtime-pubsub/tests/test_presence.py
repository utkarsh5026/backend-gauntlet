"""V3 proofs — presence as soft state.

Each test maps to a "Done when ALL true" criterion in SPEC.md V3: join on
subscribe, leave on unsubscribe, removal on every disconnect path, heartbeat +
TTL for the silent vanish, and no ghost members after an abrupt drop.

Expiry is tested by aging `last_seen` directly rather than by sleeping. A test
that waits out a real TTL is a test that is either slow or flaky, and the clock
is not the thing under test.
"""

from __future__ import annotations

from realtime_pubsub.presence import PresenceRegistry
from realtime_pubsub.protocol import next_conn_id


def _backdate(presence: PresenceRegistry, identity: str, seconds: float) -> None:
    """Backdate a member's last heartbeat, as if it had gone quiet.

    `members()` hands back the live `Member` objects, so mutating one here is
    exactly what a stale heartbeat looks like from the registry's side — no
    test-only API on the production class, and no sleeping through a real TTL.
    """
    for topic in presence.topics():
        for member in presence.members(topic):
            if member.identity == identity:
                member.last_seen -= seconds


def test_join_two_conns_lists_both_identities() -> None:
    presence = PresenceRegistry()
    a, b = next_conn_id(), next_conn_id()

    presence.join("room1", a, "alice")
    presence.join("room1", b, "bob")

    assert sorted(presence.identities("room1")) == ["alice", "bob"]


def test_leave_removes_only_that_member() -> None:
    presence = PresenceRegistry()
    a, b = next_conn_id(), next_conn_id()

    presence.join("room1", a, "alice")
    presence.join("room1", b, "bob")
    presence.leave("room1", a)

    assert presence.identities("room1") == ["bob"]


def test_leaving_last_member_prunes_the_topic() -> None:
    presence = PresenceRegistry()
    a = next_conn_id()

    presence.join("room1", a, "alice")
    presence.leave("room1", a)

    assert presence.members("room1") == []
    assert presence.member_count() == 0


def test_disconnect_removes_the_conn_from_every_topic() -> None:
    """The anti-ghost catch-all — an abrupt drop still leaves every room."""
    presence = PresenceRegistry()
    a, b = next_conn_id(), next_conn_id()

    presence.join("room1", a, "alice")
    presence.join("room2", a, "alice")
    presence.join("room1", b, "bob")

    presence.disconnect(a)

    assert presence.identities("room1") == ["bob"]
    assert presence.members("room2") == []


def test_members_of_an_unknown_topic_is_empty() -> None:
    presence = PresenceRegistry()
    assert presence.members("nobody-here") == []


def test_rejoining_refreshes_the_identity() -> None:
    presence = PresenceRegistry()
    a = next_conn_id()

    presence.join("room1", a, "alice")
    presence.join("room1", a, "alice-v2")

    assert presence.identities("room1") == ["alice-v2"]


def test_sweep_reports_nothing_when_everyone_is_fresh() -> None:
    presence = PresenceRegistry()
    presence.join("room1", next_conn_id(), "alice")

    assert presence.sweep(30.0) == []
    assert presence.identities("room1") == ["alice"]


def test_sweep_evicts_the_stale_and_reports_the_survivors() -> None:
    presence = PresenceRegistry()
    a, b = next_conn_id(), next_conn_id()
    presence.join("room1", a, "alice")
    presence.join("room1", b, "bob")
    _backdate(presence, "bob", 60.0)

    changed = presence.sweep(30.0)

    assert len(changed) == 1
    topic, survivors = changed[0]
    assert topic == "room1"
    assert [m.identity for m in survivors] == ["alice"]
    assert presence.identities("room1") == ["alice"]


def test_sweep_reports_an_empty_room_when_it_fully_clears() -> None:
    presence = PresenceRegistry()
    a = next_conn_id()
    presence.join("room1", a, "alice")
    _backdate(presence, "alice", 60.0)

    changed = presence.sweep(30.0)

    assert len(changed) == 1
    assert changed[0] == ("room1", [])
    assert presence.members("room1") == []


def test_touch_keeps_a_member_from_being_swept() -> None:
    """Heartbeat + TTL: an entry refreshed inside the window survives."""
    presence = PresenceRegistry()
    a = next_conn_id()
    presence.join("room1", a, "alice")
    _backdate(presence, "alice", 60.0)
    presence.touch(a)

    assert presence.sweep(30.0) == []
    assert presence.identities("room1") == ["alice"]


def test_touch_on_an_unknown_conn_is_a_noop() -> None:
    presence = PresenceRegistry()
    a = next_conn_id()
    presence.join("room1", a, "alice")

    presence.touch(next_conn_id())

    assert presence.identities("room1") == ["alice"]
    assert presence.sweep(30.0) == []


def test_touch_refreshes_the_conn_in_every_topic() -> None:
    presence = PresenceRegistry()
    a = next_conn_id()
    presence.join("room1", a, "alice")
    presence.join("room2", a, "alice")
    _backdate(presence, "alice", 60.0)
    presence.touch(a)

    assert presence.sweep(30.0) == []
    assert presence.identities("room1") == ["alice"]
    assert presence.identities("room2") == ["alice"]


def test_touch_only_refreshes_the_targeted_conn() -> None:
    presence = PresenceRegistry()
    a, b = next_conn_id(), next_conn_id()
    presence.join("room1", a, "alice")
    presence.join("room1", b, "bob")
    _backdate(presence, "alice", 60.0)
    _backdate(presence, "bob", 60.0)
    presence.touch(a)

    changed = presence.sweep(30.0)

    assert len(changed) == 1
    topic, survivors = changed[0]
    assert topic == "room1"
    assert [m.identity for m in survivors] == ["alice"]
