//! V2 — Per-connection backpressure (the slow-consumer problem).
//!
//! Every connection gets one **bounded outbound mailbox**. The hub pushes
//! broadcasts into the [`Mailbox`] (the sending half); the connection's writer
//! task drains the [`Outbox`] (the receiving half) to the socket as fast as that
//! client's TCP can take them.
//!
//! Bounded is the whole point. If a client reads slowly while messages keep
//! arriving, the mailbox fills — and [`Mailbox::deliver`] has to make the call
//! the SPEC is about: block the publisher (head-of-line blocking — usually
//! wrong for fan-out), or stay lossy and drop / disconnect. `DropNewest` and
//! `Disconnect` ride on a bounded `tokio::mpsc`, which gives you `try_send` for
//! free. `DropOldest` can't use that substrate: a plain `mpsc` only lets the
//! *receiver* pop from the front, but eviction has to happen from the
//! *producer* side (`deliver` is called by the hub, not the writer task). So
//! [`Mailbox`]'s storage is one of two [`Backend`]s, picked per connection from
//! [`OverflowPolicy`] — an `mpsc` channel for the two policies it suits, or a
//! shared ring buffer (`VecDeque` behind a lock, woken via `Notify`) for
//! `DropOldest`. Filling in that second backend's actual logic is the exercise.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Weak};

use parking_lot::Mutex;
use tokio::sync::{mpsc, Notify};

use crate::metrics::MESSAGES_DROPPED_TOTAL;
use crate::protocol::ServerMessage;

fn record_dropped(policy: OverflowPolicy) {
    metrics::counter!(MESSAGES_DROPPED_TOTAL, "policy" => policy.metric_label()).increment(1);
}

/// What to do when a connection's outbox is full. Parsed from `OVERFLOW_POLICY`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OverflowPolicy {
    /// Drop the message being delivered; keep the buffered backlog.
    DropNewest,
    /// Evict the oldest buffered message to make room for this one.
    DropOldest,
    /// Treat a full outbox as a too-slow client and tear the connection down.
    Disconnect,
}

impl std::str::FromStr for OverflowPolicy {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim().to_ascii_lowercase().as_str() {
            "drop_newest" | "drop-newest" => Ok(Self::DropNewest),
            "drop_oldest" | "drop-oldest" => Ok(Self::DropOldest),
            "disconnect" => Ok(Self::Disconnect),
            other => Err(format!("unknown overflow policy: {other}")),
        }
    }
}

impl OverflowPolicy {
    /// Stable Prometheus label value for [`crate::metrics::MESSAGES_DROPPED_TOTAL`].
    pub fn metric_label(self) -> &'static str {
        match self {
            Self::DropNewest => "drop_newest",
            Self::DropOldest => "drop_oldest",
            Self::Disconnect => "disconnect",
        }
    }
}

/// The result of trying to hand a message to one connection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliverOutcome {
    /// Queued for sending.
    Delivered,
    /// Dropped per the overflow policy (count this — it's the V2 metric).
    Dropped,
    /// The outbox is full and the policy says disconnect this client.
    Disconnect,
}

/// Shared storage for the `DropOldest` (`Ring`) backend: the queue itself,
/// the `Notify` used to wake a sleeping [`Outbox::recv`], and a live count of
/// [`Mailbox`] clones still able to `deliver` into it.
///
/// `senders` is what lets the receiver detect "closed" the way an `mpsc`
/// does — see [`SenderGuard`] for how it stays accurate across clones/drops.
pub(crate) struct RingState {
    queue: Mutex<VecDeque<ServerMessage>>,
    notify: Notify,
    senders: AtomicUsize,
}

/// The piece of [`Backend::Ring`] that owns sender-liveness bookkeeping.
///
/// Wrapping just this `Arc<RingState>` (rather than hand-writing `Clone` /
/// `Drop` on [`Mailbox`] itself) keeps the bookkeeping in one place: every
/// `Mailbox` clone using the `Ring` backend carries exactly one
/// `SenderGuard`, so `Backend`'s and `Mailbox`'s derived `Clone` impls
/// automatically call [`SenderGuard::clone`] below, and letting a clone (or
/// the original) drop automatically calls [`SenderGuard::drop`] below —
/// no manual `Clone`/`Drop` needed anywhere else.
struct SenderGuard(Arc<RingState>);

impl SenderGuard {
    fn state(&self) -> &RingState {
        &self.0
    }
}

impl Clone for SenderGuard {
    fn clone(&self) -> Self {
        self.0.senders.fetch_add(1, Ordering::Relaxed);
        Self(Arc::clone(&self.0))
    }
}

impl Drop for SenderGuard {
    fn drop(&mut self) {
        // `fetch_sub` returns the *previous* value, so `1` here means this
        // was the last live sender. `notify_waiters` (not `notify_one`) is
        // required: `Outbox::recv` creates its `Notified` future *before*
        // re-checking `senders`, and per `Notify`'s docs a `notify_waiters`
        // call is guaranteed to reach any `Notified` future created before
        // it — even if that future hasn't been polled yet. `notify_one`
        // carries no such guarantee pre-poll, so it could be missed here.
        if self.0.senders.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.0.notify.notify_waiters();
        }
    }
}

/// A connection's shared queue storage. `Channel` backs `DropNewest` /
/// `Disconnect` (an `mpsc` gives us non-blocking `try_send` for free).
/// `DropOldest` needs `Ring`: evicting the oldest entry means reaching the
/// front of the queue from the producer side, which `mpsc` doesn't expose —
/// so both "ends" here share the same [`RingState`] directly, coordinated by
/// its `Notify` so the consumer can still wait asynchronously instead of
/// polling. The `Weak<()>` mirrors [`Outbox::Ring`]'s `Arc<()>` — a sender
/// asks it "is the receiver still around?" before enqueueing.
#[derive(Clone)]
enum Backend {
    Channel(mpsc::Sender<ServerMessage>),
    Ring(SenderGuard, Weak<()>),
}

/// The sending half, held by the hub (cloned into each topic this connection
/// subscribes to). Cloning is cheap — it shares the one underlying queue.
#[derive(Clone)]
pub struct Mailbox {
    backend: Backend,
    policy: OverflowPolicy,
    capacity: usize,
}

/// The receiving half, owned by the connection's writer task. Which variant
/// it is always matches the [`Mailbox`] it was created alongside in
/// [`mailbox`] — the two halves share a [`RingState`] (for `Ring`) or an
/// `mpsc` channel (for `Channel`).
pub enum Outbox {
    Channel(mpsc::Receiver<ServerMessage>),
    Ring(Arc<RingState>, Arc<()>),
}

/// Create a connection's mailbox/outbox pair with a bounded capacity. Which
/// [`Backend`] gets built depends on `policy`.
pub fn mailbox(capacity: usize, policy: OverflowPolicy) -> (Mailbox, Outbox) {
    let capacity = capacity.max(1);
    match policy {
        OverflowPolicy::DropNewest | OverflowPolicy::Disconnect => {
            let (tx, rx) = mpsc::channel(capacity);
            (
                Mailbox {
                    backend: Backend::Channel(tx),
                    policy,
                    capacity,
                },
                Outbox::Channel(rx),
            )
        }
        OverflowPolicy::DropOldest => {
            let state = Arc::new(RingState {
                queue: Mutex::new(VecDeque::with_capacity(capacity)),
                notify: Notify::new(),
                senders: AtomicUsize::new(1),
            });
            let alive = Arc::new(());
            (
                Mailbox {
                    backend: Backend::Ring(SenderGuard(Arc::clone(&state)), Arc::downgrade(&alive)),
                    policy,
                    capacity,
                },
                Outbox::Ring(state, alive),
            )
        }
    }
}

impl Mailbox {
    /// Try to enqueue `msg` for this connection **without ever blocking the
    /// publisher**. Apply [`self.policy`](OverflowPolicy) when the outbox is full
    /// and report the [`DeliverOutcome`] so the caller can count drops / trigger
    /// a disconnect.
    pub fn deliver(&self, msg: ServerMessage) -> DeliverOutcome {
        match &self.backend {
            Backend::Channel(tx) => match tx.try_send(msg) {
                Ok(()) => DeliverOutcome::Delivered,
                Err(mpsc::error::TrySendError::Full(_)) => match self.policy {
                    OverflowPolicy::DropNewest => {
                        record_dropped(self.policy);
                        DeliverOutcome::Dropped
                    }
                    OverflowPolicy::Disconnect => DeliverOutcome::Disconnect,
                    OverflowPolicy::DropOldest => {
                        unreachable!("DropOldest never constructs Backend::Channel")
                    }
                },
                Err(mpsc::error::TrySendError::Closed(_)) => DeliverOutcome::Disconnect,
            },
            Backend::Ring(guard, receiver_alive) => {
                if receiver_alive.upgrade().is_none() {
                    return DeliverOutcome::Disconnect;
                }
                let state = guard.state();
                let mut queue_guard = state.queue.lock();
                if queue_guard.len() >= self.capacity {
                    queue_guard.pop_front();
                    record_dropped(OverflowPolicy::DropOldest);
                }
                queue_guard.push_back(msg);
                state.notify.notify_one();
                DeliverOutcome::Delivered
            }
        }
    }
}

impl Outbox {
    /// Wait for and return the next queued message, or `None` once every
    /// [`Mailbox`] for this connection is gone and nothing is left buffered.
    pub async fn recv(&mut self) -> Option<ServerMessage> {
        match self {
            Outbox::Channel(rx) => rx.recv().await,
            Outbox::Ring(state, _alive) => loop {
                // Registered *before* the checks below so a `SenderGuard`
                // drop's `notify_waiters` — which fires the moment `senders`
                // hits zero — can never land in the gap between "we checked
                // and it wasn't zero yet" and "we started waiting". See
                // `SenderGuard::drop` for why `notify_waiters` specifically
                // makes that guarantee.
                let notified = state.notify.notified();
                {
                    let mut queue_guard = state.queue.lock();
                    if !queue_guard.is_empty() {
                        return Some(queue_guard.pop_front().unwrap());
                    }
                }
                if state.senders.load(Ordering::Acquire) == 0 {
                    return None;
                }
                notified.await;
            },
        }
    }

    /// Non-blocking poll: return the next message if one is already queued.
    ///
    /// Only exercised by `hub`'s test suite today, which a plain (non-test)
    /// build can't see — hence the `dead_code` override below.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn try_recv(&mut self) -> Result<ServerMessage, mpsc::error::TryRecvError> {
        match self {
            Outbox::Channel(rx) => rx.try_recv(),
            Outbox::Ring(state, _alive) => {
                let mut queue_guard = state.queue.lock();
                if !queue_guard.is_empty() {
                    return Ok(queue_guard.pop_front().unwrap());
                }
                drop(queue_guard);
                if state.senders.load(Ordering::Acquire) == 0 {
                    Err(mpsc::error::TryRecvError::Disconnected)
                } else {
                    Err(mpsc::error::TryRecvError::Empty)
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use super::*;

    fn msg(payload: &str) -> ServerMessage {
        ServerMessage::Message {
            topic: "room".to_string(),
            payload: serde_json::Value::String(payload.to_string()),
        }
    }

    #[test]
    fn drop_newest_drops_once_full_and_buffer_stays_bounded() {
        let (mbox, mut outbox) = mailbox(2, OverflowPolicy::DropNewest);

        assert_eq!(mbox.deliver(msg("a")), DeliverOutcome::Delivered);
        assert_eq!(mbox.deliver(msg("b")), DeliverOutcome::Delivered);
        // Outbox is full at capacity 2 — the newest message is shed, not buffered.
        assert_eq!(mbox.deliver(msg("c")), DeliverOutcome::Dropped);

        assert_eq!(outbox.try_recv().unwrap(), msg("a"));
        assert_eq!(outbox.try_recv().unwrap(), msg("b"));
        assert!(
            outbox.try_recv().is_err(),
            "\"c\" must not have been buffered"
        );
    }

    #[test]
    fn disconnect_policy_signals_disconnect_once_full_and_never_evicts() {
        let (mbox, mut outbox) = mailbox(1, OverflowPolicy::Disconnect);

        assert_eq!(mbox.deliver(msg("first")), DeliverOutcome::Delivered);
        assert_eq!(mbox.deliver(msg("second")), DeliverOutcome::Disconnect);

        // Disconnect never makes room for the new message by evicting the old one.
        assert_eq!(outbox.try_recv().unwrap(), msg("first"));
        assert!(outbox.try_recv().is_err());
    }

    #[test]
    fn drop_oldest_evicts_the_front_so_newest_survive_and_buffer_stays_bounded() {
        let (mbox, mut outbox) = mailbox(2, OverflowPolicy::DropOldest);

        // `Ring` never reports `Dropped` to the publisher — it always makes
        // room by evicting, so every call here reports `Delivered`.
        for payload in ["a", "b", "c", "d"] {
            assert_eq!(mbox.deliver(msg(payload)), DeliverOutcome::Delivered);
        }

        // Only the last `capacity` messages survive; "a" and "b" were evicted
        // from the front to make room, oldest first.
        assert_eq!(outbox.try_recv().unwrap(), msg("c"));
        assert_eq!(outbox.try_recv().unwrap(), msg("d"));
        assert!(outbox.try_recv().is_err());
    }

    #[test]
    fn fast_drainer_never_sees_a_dropped_outcome_under_sustained_load() {
        let (mbox, mut outbox) = mailbox(1, OverflowPolicy::DropNewest);

        // Capacity 1 would overflow immediately under any backlog — but a
        // drainer that keeps up (drains between every delivery) never lets
        // one form, so every outcome must be `Delivered`.
        for i in 0..1_000 {
            let m = msg(&format!("msg-{i}"));
            assert_eq!(mbox.deliver(m.clone()), DeliverOutcome::Delivered);
            assert_eq!(outbox.try_recv().unwrap(), m);
        }
    }

    #[test]
    fn deliver_never_blocks_on_a_stalled_reader_under_any_policy() {
        for policy in [
            OverflowPolicy::DropNewest,
            OverflowPolicy::DropOldest,
            OverflowPolicy::Disconnect,
        ] {
            let (mbox, _outbox) = mailbox(1, policy);

            let start = Instant::now();
            for i in 0..10_000 {
                // Once `Disconnect` fires the "connection" is logically gone,
                // but nothing stops the publisher from calling deliver again
                // (the hub reaps it lazily) — so this loop must still return
                // promptly rather than block, under every policy.
                mbox.deliver(msg(&format!("msg-{i}")));
            }
            let elapsed = start.elapsed();

            assert!(
                elapsed < Duration::from_secs(2),
                "deliver under {policy:?} took {elapsed:?} against a stalled reader — looks blocked"
            );
        }
    }

    #[test]
    fn stalled_reader_drop_counter_climbs_while_fast_subscriber_is_unaffected() {
        // Mirrors the V2 payoff: one connection stalls, another stays caught
        // up, and the stalled one's losses never touch the fast one.
        let (stalled_mbox, _stalled_outbox) = mailbox(1, OverflowPolicy::DropNewest);
        let (fast_mbox, mut fast_outbox) = mailbox(1_000, OverflowPolicy::DropNewest);

        const N: usize = 500;
        let mut dropped = 0;
        for i in 0..N {
            let m = msg(&format!("msg-{i}"));
            if stalled_mbox.deliver(m.clone()) == DeliverOutcome::Dropped {
                dropped += 1;
            }
            assert_eq!(fast_mbox.deliver(m), DeliverOutcome::Delivered);
        }

        // The first delivery fills the stalled mailbox's one slot; every
        // delivery after that is shed — the drop counter climbs in lockstep
        // with the backlog, it's never silent.
        assert_eq!(dropped, N - 1);

        let mut received = 0;
        while fast_outbox.try_recv().is_ok() {
            received += 1;
        }
        assert_eq!(received, N, "fast subscriber must receive every message");
    }

    #[test]
    fn channel_backend_reports_disconnect_once_receiver_is_dropped() {
        let (mbox, outbox) = mailbox(4, OverflowPolicy::DropNewest);
        drop(outbox);

        assert_eq!(mbox.deliver(msg("hi")), DeliverOutcome::Disconnect);
    }

    #[test]
    fn ring_backend_reports_disconnect_once_receiver_is_dropped() {
        let (mbox, outbox) = mailbox(4, OverflowPolicy::DropOldest);
        drop(outbox);

        assert_eq!(mbox.deliver(msg("hi")), DeliverOutcome::Disconnect);
    }

    #[tokio::test]
    async fn ring_backend_recv_returns_survivors_in_order_after_overflow() {
        let (mbox, mut outbox) = mailbox(2, OverflowPolicy::DropOldest);

        for payload in ["a", "b", "c", "d"] {
            mbox.deliver(msg(payload));
        }

        assert_eq!(outbox.recv().await.unwrap(), msg("c"));
        assert_eq!(outbox.recv().await.unwrap(), msg("d"));
    }

    #[tokio::test]
    async fn ring_backend_recv_drains_buffered_message_then_closes() {
        // Regression guard for the race `SenderGuard::drop` documents: a
        // message delivered right before the last sender drops must still be
        // observed by `recv` before it reports the outbox closed.
        let (mbox, mut outbox) = mailbox(2, OverflowPolicy::DropOldest);
        assert_eq!(mbox.deliver(msg("last")), DeliverOutcome::Delivered);
        drop(mbox);

        assert_eq!(outbox.recv().await.unwrap(), msg("last"));
        assert_eq!(outbox.recv().await, None);
    }
}
