//! V3 — Presence: who is currently in each topic.
//!
//! Presence is **soft state** — only ever an approximation of reality that must
//! converge as connections come and go. The registry below tracks, per topic,
//! which connections are present and the display identity each one claimed.
//!
//! The easy half is the clean lifecycle: `join` on subscribe, `leave` on
//! unsubscribe. The hard half is *absence*: a client whose network drops never
//! sends a leave. A clean implementation removes a connection on every disconnect
//! path; a robust one adds a **heartbeat + TTL** and sweeps entries that haven't
//! been refreshed — see the stretch TODO at the bottom.

use std::collections::HashMap;

use parking_lot::RwLock;

use crate::protocol::{ConnId, Topic};

/// Per-topic membership. Like the hub, the inner shape is a starting point: if
/// you add heartbeats you'll want to store a last-seen `Instant` alongside the
/// identity so a sweep can expire stale entries.
#[derive(Default)]
pub struct PresenceRegistry {
    members: RwLock<HashMap<Topic, HashMap<ConnId, String>>>,
}

impl PresenceRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn join(&self, topic: &str, conn: ConnId, identity: String) {
        let mut members = self.members.write();
        members
            .entry(topic.to_string())
            .or_default()
            .insert(conn, identity);
    }

    pub fn leave(&self, topic: &str, conn: ConnId) {
        let mut members = self.members.write();
        members.entry(topic.to_string()).or_default().remove(&conn);
        if members.get(topic).unwrap().is_empty() {
            members.remove(topic);
        }
    }

    pub fn members(&self, topic: &str) -> Vec<String> {
        let members = self.members.read();
        if members.contains_key(topic) {
            members.get(topic).unwrap().values().cloned().collect()
        } else {
            Vec::new()
        }
    }

    pub fn disconnect(&self, conn: ConnId) {
        let mut members = self.members.write();
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
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sorted(members: Vec<String>) -> Vec<String> {
        let mut members = members;
        members.sort();
        members
    }

    #[test]
    fn join_two_conns_returns_both_identities() {
        let presence = PresenceRegistry::new();
        let a = ConnId::next();
        let b = ConnId::next();

        presence.join("room1", a, "alice".into());
        presence.join("room1", b, "bob".into());

        assert_eq!(
            sorted(presence.members("room1")),
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

        assert_eq!(presence.members("room1"), vec!["bob".to_string()]);
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

        assert_eq!(presence.members("room1"), vec!["bob".to_string()]);
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

        assert_eq!(presence.members("room1"), vec!["alice-v2".to_string()]);
    }
}
