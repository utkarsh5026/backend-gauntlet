//! V3 — Presence: who is currently in each topic.
//!
//! Presence is **soft state** — only ever an approximation of reality that must
//! converge as connections come and go. The [`PresenceRegistry`] tracks, per
//! topic, which connections are present and the display identity each one
//! claimed.
//!
//! The easy half is the clean lifecycle: [`PresenceRegistry::join`] on
//! subscribe, [`PresenceRegistry::leave`] on unsubscribe. The hard half is
//! *absence*: a client whose network drops never sends a leave.
//! [`PresenceRegistry::disconnect`] covers every observed teardown path;
//! [`PresenceRegistry::touch`] plus [`PresenceRegistry::sweep`] handle silent
//! vanish via heartbeat + TTL.

use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use parking_lot::RwLock;

use crate::protocol::{ConnId, Topic};

/// One connection's membership in a topic: display name plus liveness stamp.
///
/// `last_seen` is refreshed by [`Member::refresh`] (via
/// [`PresenceRegistry::touch`]) so [`PresenceRegistry::sweep`] can expire
/// entries that stop heartbeating.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Member {
    identity: String,
    last_seen: Instant,
}

impl Member {
    /// Create a member with `identity` and `last_seen` set to now.
    pub fn new(identity: String) -> Self {
        Self {
            identity,
            last_seen: Instant::now(),
        }
    }

    /// Display identity claimed for this membership (wire-facing name).
    pub fn identity(&self) -> &str {
        &self.identity
    }

    /// Mark this member as seen now (heartbeat / any liveness proof).
    pub fn refresh(&mut self) {
        self.last_seen = Instant::now();
    }

    #[cfg(test)]
    fn age_for_test(&mut self, by: Duration) {
        self.last_seen -= by;
    }
}

/// Per-topic presence registry: who is currently in each room.
///
/// Orthogonal to the fan-out [`crate::hub::Hub`]: the hub answers "who receives
/// messages?", this answers "who appears in the room list?". Interior-mutable
/// (`&self` methods) so it can live behind an `Arc` in `AppState`.
#[derive(Default)]
pub struct PresenceRegistry {
    topic_members: RwLock<HashMap<Topic, HashMap<ConnId, Member>>>,
}

impl PresenceRegistry {
    /// Create an empty registry with no topics and no members.
    pub fn new() -> Self {
        Self::default()
    }

    /// Record `conn` as present in `topic` under `identity`.
    ///
    /// Re-joining the same `conn` replaces the previous [`Member`] (new
    /// identity and a fresh `last_seen`). Typical call site: subscribe.
    pub fn join(&self, topic: &str, conn: ConnId, identity: String) {
        let mut members = self.topic_members.write();
        members
            .entry(topic.to_string())
            .or_default()
            .insert(conn, Member::new(identity));
    }

    /// Remove `conn` from `topic`, pruning the topic when it becomes empty.
    ///
    /// Idempotent: a missing topic or unknown `conn` is a no-op aside from
    /// briefly ensuring the topic entry exists before removal.
    pub fn leave(&self, topic: &str, conn: ConnId) {
        let mut members = self.topic_members.write();
        members.entry(topic.to_string()).or_default().remove(&conn);
        if members.get(topic).unwrap().is_empty() {
            members.remove(topic);
        }
    }

    /// Snapshot the members currently present in `topic`.
    ///
    /// Returns an empty vec when the topic is unknown or has been pruned —
    /// both mean "nobody here" for soft-state presence.
    pub fn members(&self, topic: &str) -> Vec<Member> {
        let members = self.topic_members.read();
        if members.contains_key(topic) {
            members.get(topic).unwrap().values().cloned().collect()
        } else {
            Vec::new()
        }
    }

    /// Remove `conn` from every topic it appears in, pruning empty topics.
    ///
    /// The anti-ghost catch-all: call on every WebSocket teardown path (clean
    /// close, error, abrupt drop). Idempotent if `conn` was never present.
    pub fn disconnect(&self, conn: ConnId) {
        let mut members = self.topic_members.write();
        let mut keys_to_remove = Vec::new();
        for (topic, members) in members.iter_mut() {
            members.remove(&conn);
            if members.is_empty() {
                keys_to_remove.push(topic.clone());
            }
        }
        for topic in keys_to_remove {
            members.remove(&topic);
        }
    }

    /// Refresh `last_seen` for `conn` in every topic it belongs to.
    ///
    /// No-op if `conn` is not present anywhere. Wire this from WebSocket
    /// ping/pong (or any client traffic) so live sockets stay under the TTL.
    pub fn touch(&self, conn: ConnId) {
        let mut members = self.topic_members.write();
        for (_, members) in members.iter_mut() {
            if let Some(member) = members.get_mut(&conn) {
                member.refresh();
            }
        }
    }

    #[cfg(test)]
    fn age(&self, conn: ConnId, by: Duration) {
        let mut members = self.topic_members.write();
        for topic_members in members.values_mut() {
            if let Some(member) = topic_members.get_mut(&conn) {
                member.age_for_test(by);
            }
        }
    }

    /// Evict members whose `last_seen` is at least `ttl` old.
    ///
    /// Returns only topics that lost at least one member, each paired with the
    /// **remaining** members after eviction (empty if the room was fully
    /// cleared). Unchanged topics are omitted so a background task can
    /// broadcast [`crate::protocol::ServerMessage::Presence`] only when needed.
    ///
    /// Intended to be called on an interval from a `tokio` task; this method
    /// itself is synchronous and lock-scoped.
    pub fn sweep(&self, ttl: Duration) -> Vec<(Topic, Vec<Member>)> {
        let mut topic_members = self.topic_members.write();
        let mut changed = Vec::new();

        topic_members.retain(|topic, members| {
            let before = members.len();
            members.retain(|_, member| member.last_seen.elapsed() < ttl);
            if members.len() != before {
                changed.push((topic.clone(), members.values().cloned().collect()));
            }
            !members.is_empty()
        });

        changed
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identities(members: Vec<Member>) -> Vec<String> {
        let mut ids: Vec<String> = members
            .into_iter()
            .map(|m| m.identity().to_string())
            .collect();
        ids.sort();
        ids
    }

    #[test]
    fn join_two_conns_returns_both_identities() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        let b = ConnId::next();

        presence.join("room1", a, "alice".into());
        presence.join("room1", b, "bob".into());

        assert_eq!(
            identities(presence.members("room1")),
            vec!["alice".to_string(), "bob".to_string()]
        );
    }

    #[test]
    fn leave_one_leaves_the_other() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        let b = ConnId::next();

        presence.join("room1", a, "alice".into());
        presence.join("room1", b, "bob".into());
        presence.leave("room1", a);

        assert_eq!(
            identities(presence.members("room1")),
            vec!["bob".to_string()]
        );
    }

    #[test]
    fn leave_last_member_prunes_topic() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();

        presence.join("room1", a, "alice".into());
        presence.leave("room1", a);

        assert!(presence.members("room1").is_empty());
    }

    #[test]
    fn disconnect_removes_conn_from_every_topic() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        let b = ConnId::next();

        presence.join("room1", a, "alice".into());
        presence.join("room2", a, "alice".into());
        presence.join("room1", b, "bob".into());

        presence.disconnect(a);

        assert_eq!(
            identities(presence.members("room1")),
            vec!["bob".to_string()]
        );
        assert!(presence.members("room2").is_empty());
    }

    #[test]
    fn members_on_unknown_topic_is_empty() {
        let presence = PresenceRegistry::new();
        assert!(presence.members("nobody-here").is_empty());
    }

    #[test]
    fn join_same_conn_refreshes_identity() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();

        presence.join("room1", a, "alice".into());
        presence.join("room1", a, "alice-v2".into());

        assert_eq!(
            identities(presence.members("room1")),
            vec!["alice-v2".to_string()]
        );
    }

    #[test]
    fn sweep_returns_nothing_when_everyone_is_fresh() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        presence.join("room1", a, "alice".into());

        let changed = presence.sweep(Duration::from_secs(30));
        assert!(changed.is_empty());
        assert_eq!(
            identities(presence.members("room1")),
            vec!["alice".to_string()]
        );
    }

    #[test]
    fn sweep_returns_survivors_when_someone_expires() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        let b = ConnId::next();
        presence.join("room1", a, "alice".into());
        presence.join("room1", b, "bob".into());
        presence.age(b, Duration::from_secs(60));

        let changed = presence.sweep(Duration::from_secs(30));
        assert_eq!(changed.len(), 1);
        assert_eq!(changed[0].0, "room1");
        assert_eq!(identities(changed[0].1.clone()), vec!["alice".to_string()]);
        assert_eq!(
            identities(presence.members("room1")),
            vec!["alice".to_string()]
        );
    }

    #[test]
    fn sweep_returns_empty_members_when_room_fully_cleared() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        presence.join("room1", a, "alice".into());
        presence.age(a, Duration::from_secs(60));

        let changed = presence.sweep(Duration::from_secs(30));
        assert_eq!(changed.len(), 1);
        assert_eq!(changed[0].0, "room1");
        assert!(changed[0].1.is_empty());
        assert!(presence.members("room1").is_empty());
    }

    #[test]
    fn touch_keeps_member_from_being_swept() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        presence.join("room1", a, "alice".into());
        presence.age(a, Duration::from_secs(60));
        presence.touch(a);

        let changed = presence.sweep(Duration::from_secs(30));
        assert!(changed.is_empty());
        assert_eq!(
            identities(presence.members("room1")),
            vec!["alice".to_string()]
        );
    }

    #[test]
    fn touch_unknown_conn_is_a_noop() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        let ghost = ConnId::next();
        presence.join("room1", a, "alice".into());

        presence.touch(ghost);

        assert_eq!(
            identities(presence.members("room1")),
            vec!["alice".to_string()]
        );
        assert!(presence.sweep(Duration::from_secs(30)).is_empty());
    }

    #[test]
    fn touch_refreshes_conn_in_every_topic() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        presence.join("room1", a, "alice".into());
        presence.join("room2", a, "alice".into());
        presence.age(a, Duration::from_secs(60));
        presence.touch(a);

        assert!(presence.sweep(Duration::from_secs(30)).is_empty());
        assert_eq!(
            identities(presence.members("room1")),
            vec!["alice".to_string()]
        );
        assert_eq!(
            identities(presence.members("room2")),
            vec!["alice".to_string()]
        );
    }

    #[test]
    fn touch_only_refreshes_the_targeted_conn() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        let b = ConnId::next();
        presence.join("room1", a, "alice".into());
        presence.join("room1", b, "bob".into());
        presence.age(a, Duration::from_secs(60));
        presence.age(b, Duration::from_secs(60));
        presence.touch(a);

        let changed = presence.sweep(Duration::from_secs(30));
        assert_eq!(changed.len(), 1);
        assert_eq!(changed[0].0, "room1");
        assert_eq!(identities(changed[0].1.clone()), vec!["alice".to_string()]);
        assert_eq!(
            identities(presence.members("room1")),
            vec!["alice".to_string()]
        );
    }
}
