//! V1 — The fan-out hub: the in-process pub/sub core, from scratch.
//!
//! This is the registry you'd normally get from `tokio::sync::broadcast` or an
//! actor framework. It maps **topic → subscribers** and, on a publish, hands the
//! message to every current subscriber's [`Mailbox`].
//!
//! The map itself is the easy part. The hard parts are concurrency and lock
//! discipline: thousands of tasks call `subscribe` / `publish` / `disconnect` at
//! once, and the cardinal rule is **never hold the lock while you send** — copy
//! out the subscribers you need to reach, release the lock, *then* deliver. Hold
//! the lock across a `deliver` to a slow client and you've serialised the entire
//! hub behind that one client.

use std::collections::HashMap;

use parking_lot::RwLock;

use crate::backpressure::{DeliverOutcome, Mailbox};
use crate::protocol::{ConnId, ServerMessage};

/// The in-process subscription registry.
///
/// The field below is a reasonable **starting point**, not a mandate — part of
/// V1 is deciding whether one `RwLock` over the whole map is good enough or
/// whether you want per-topic locking (or a sharded / lock-free structure) so
/// publishes to different topics don't contend.
#[derive(Default)]
pub struct Hub {
    topics: RwLock<HashMap<String, HashMap<ConnId, Mailbox>>>,
}

impl Hub {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn subscribe(&self, topic: &str, conn: ConnId, mailbox: Mailbox) {
        let mut topics = self.topics.write();
        topics
            .entry(topic.to_string())
            .or_default()
            .insert(conn, mailbox);
    }

    pub fn unsubscribe(&self, topic: &str, conn: ConnId) {
        let mut topics = self.topics.write();
        if !topics.contains_key(topic) {
            return;
        }

        topics.entry(topic.to_string()).and_modify(|mailboxes| {
            mailboxes.remove(&conn);
            if mailboxes.is_empty() {
                mailboxes.clear();
            }
        });

        if topics.get(topic).unwrap().is_empty() {
            topics.remove(topic);
        }
    }

    /// Deliver `msg` to every current subscriber of `topic`. Returns how many
    /// subscribers it reached (i.e. [`DeliverOutcome::Delivered`]).
    pub fn publish(&self, topic: &str, msg: ServerMessage) -> usize {
        let mailboxes = { self.topics.read().get(topic).cloned().unwrap_or_default() };
        if mailboxes.is_empty() {
            return 0;
        }

        let mut delivered = 0;
        let mut disconnects = Vec::new();
        for (conn, mailbox) in mailboxes {
            match mailbox.deliver(msg.clone()) {
                DeliverOutcome::Delivered => delivered += 1,
                DeliverOutcome::Disconnect => disconnects.push(conn),
                DeliverOutcome::Dropped => {}
            }
        }
        disconnects
            .into_iter()
            .for_each(|conn| self.unsubscribe(topic, conn));
        delivered
    }

    /// Remove `conn` from **every** topic it joined. Called on disconnect — a
    /// dropped socket must leave nothing behind.
    pub fn disconnect(&self, conn: ConnId) {
        let mut topics = self.topics.write();
        topics.retain(|_, mailboxes| {
            mailboxes.remove(&conn);
            !mailboxes.is_empty()
        });
    }
}

#[cfg(test)]
impl Hub {
    fn subscriber_count(&self, topic: &str) -> usize {
        self.topics.read().get(topic).map(|m| m.len()).unwrap_or(0)
    }

    fn topic_count(&self) -> usize {
        self.topics.read().len()
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::thread;

    use super::*;
    use crate::backpressure::{mailbox, OverflowPolicy};

    fn broadcast(topic: &str, payload: &str) -> ServerMessage {
        ServerMessage::Message {
            topic: topic.to_string(),
            payload: serde_json::Value::String(payload.to_string()),
        }
    }

    #[test]
    fn publish_to_unknown_topic_returns_zero() {
        let hub = Hub::new();
        assert_eq!(hub.publish("empty", broadcast("empty", "hi")), 0);
    }

    #[test]
    fn subscribe_and_publish_reaches_all_subscribers() {
        let hub = Hub::new();
        let conn_a = ConnId::next();
        let conn_b = ConnId::next();
        let (mbox_a, mut out_a) = mailbox(4, OverflowPolicy::DropNewest);
        let (mbox_b, mut out_b) = mailbox(4, OverflowPolicy::DropNewest);

        hub.subscribe("room", conn_a, mbox_a);
        hub.subscribe("room", conn_b, mbox_b);

        assert_eq!(hub.publish("room", broadcast("room", "hello")), 2);
        assert_eq!(out_a.try_recv().unwrap(), broadcast("room", "hello"));
        assert_eq!(out_b.try_recv().unwrap(), broadcast("room", "hello"));
    }

    #[test]
    fn subscribe_same_conn_is_idempotent() {
        let hub = Hub::new();
        let conn = ConnId::next();
        let (mbox, mut outbox) = mailbox(4, OverflowPolicy::DropNewest);

        hub.subscribe("room", conn, mbox.clone());
        hub.subscribe("room", conn, mbox);

        assert_eq!(hub.subscriber_count("room"), 1);
        assert_eq!(hub.publish("room", broadcast("room", "once")), 1);
        assert_eq!(outbox.try_recv().unwrap(), broadcast("room", "once"));
        assert!(outbox.try_recv().is_err());
    }

    #[test]
    fn unsubscribe_stops_future_deliveries() {
        let hub = Hub::new();
        let conn_a = ConnId::next();
        let conn_b = ConnId::next();
        let (mbox_a, mut out_a) = mailbox(4, OverflowPolicy::DropNewest);
        let (mbox_b, mut out_b) = mailbox(4, OverflowPolicy::DropNewest);

        hub.subscribe("room", conn_a, mbox_a);
        hub.subscribe("room", conn_b, mbox_b);
        hub.unsubscribe("room", conn_a);

        assert_eq!(hub.subscriber_count("room"), 1);
        assert_eq!(hub.publish("room", broadcast("room", "after")), 1);
        assert!(out_a.try_recv().is_err());
        assert_eq!(out_b.try_recv().unwrap(), broadcast("room", "after"));
    }

    #[test]
    fn unsubscribe_prunes_empty_topic() {
        let hub = Hub::new();
        let conn = ConnId::next();
        let (mbox, _outbox) = mailbox(4, OverflowPolicy::DropNewest);

        hub.subscribe("room", conn, mbox);
        hub.unsubscribe("room", conn);

        assert_eq!(hub.subscriber_count("room"), 0);
        assert_eq!(hub.topic_count(), 0);
        assert_eq!(hub.publish("room", broadcast("room", "ghost")), 0);
    }

    #[test]
    fn unsubscribe_unknown_topic_is_noop() {
        let hub = Hub::new();
        hub.unsubscribe("missing", ConnId::next());
        assert_eq!(hub.topic_count(), 0);
    }

    #[test]
    fn disconnect_removes_conn_from_every_topic() {
        let hub = Hub::new();
        let conn = ConnId::next();
        let other = ConnId::next();
        let (mbox, mut out) = mailbox(4, OverflowPolicy::DropNewest);
        let (other_mbox, mut other_out) = mailbox(4, OverflowPolicy::DropNewest);

        hub.subscribe("room1", conn, mbox);
        hub.subscribe("room2", conn, mailbox(4, OverflowPolicy::DropNewest).0);
        hub.subscribe("room1", other, other_mbox);

        hub.disconnect(conn);

        assert_eq!(hub.subscriber_count("room1"), 1);
        assert_eq!(hub.subscriber_count("room2"), 0);
        assert_eq!(hub.topic_count(), 1);
        assert_eq!(hub.publish("room1", broadcast("room1", "still")), 1);
        assert_eq!(hub.publish("room2", broadcast("room2", "gone")), 0);
        assert!(out.try_recv().is_err());
        assert_eq!(other_out.try_recv().unwrap(), broadcast("room1", "still"));
    }

    #[test]
    fn concurrent_subscribe_publish_unsubscribe() {
        let hub = Arc::new(Hub::new());
        let mut handles = Vec::new();

        for _ in 0..8 {
            let hub = Arc::clone(&hub);
            handles.push(thread::spawn(move || {
                for i in 0..50 {
                    let conn = ConnId::next();
                    let (mbox, _outbox) = mailbox(4, OverflowPolicy::DropNewest);
                    let topic = format!("room-{}", i % 4);
                    hub.subscribe(&topic, conn, mbox);
                    hub.publish(&topic, broadcast(&topic, "load"));
                    hub.unsubscribe(&topic, conn);
                }
            }));
        }

        for handle in handles {
            handle.join().expect("thread panicked");
        }

        assert_eq!(hub.topic_count(), 0);
    }
}
