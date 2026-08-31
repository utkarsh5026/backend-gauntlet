"""Prometheus metrics for the observability checklist (see SPEC.md -> Horizontal
checklist -> Observability).

Python shape note: the Rust side used the `metrics` facade — string constants
written to a process-global recorder, installed once from `main`. `prometheus_client`
has no facade and needs none: a metric *is* an object, created once at import and
registered on the default `REGISTRY`. Call sites import the object and call
`.inc()` / `.set()` on it. That means these are live from the moment the module
is imported, including under pytest, so a test can assert on a counter without
installing anything first.

`common_telemetry.metrics_routes()` renders that same default registry at
`GET /metrics`, so nothing here has to be wired into the app by hand.

## What's wired vs. what's a TODO
Drop counting on the publish path is wired in `backpressure.Mailbox.deliver`
(`MESSAGES_DROPPED`), and `hub.Hub.publish` records deliveries and slow-client
reaps. Remaining call sites are still **TODO** — see the SPEC's Observability
checklist:
  - `routes.dispatch` — `MESSAGES_PUBLISHED` when a client sends `publish`.
  - `routes.websocket_endpoint` — `OPEN_CONNECTIONS` up on connect, down on
    every teardown path.
  - subscribe/unsubscribe paths — refresh `SUBSCRIPTIONS` and `TOPICS`.
  - `presence.py` — `PRESENCE_MEMBERS` per topic (or one aggregate if you would
    rather not pay the cardinality).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

__all__ = [
    "MESSAGES_DELIVERED",
    "MESSAGES_DROPPED",
    "MESSAGES_PUBLISHED",
    "OPEN_CONNECTIONS",
    "PRESENCE_MEMBERS",
    "SLOW_CLIENT_DISCONNECTS",
    "SUBSCRIPTIONS",
    "TOPICS",
]

MESSAGES_PUBLISHED = Counter(
    "realtime_pubsub_messages_published_total",
    "Client-originated publishes accepted by the server",
)

MESSAGES_DELIVERED = Counter(
    "realtime_pubsub_messages_delivered_total",
    "Fan-out deliveries that reached a subscriber mailbox",
)

MESSAGES_DROPPED = Counter(
    "realtime_pubsub_messages_dropped_total",
    "Messages shed by the backpressure overflow policy, labelled by policy",
    labelnames=("policy",),
)
"""The whole point of V2. Labelled by `OverflowPolicy.value` so a dashboard can
tell a drop_oldest eviction from a drop_newest refusal."""

SLOW_CLIENT_DISCONNECTS = Counter(
    "realtime_pubsub_slow_client_disconnects_total",
    "Connections disconnected because the outbound mailbox was full or closed",
)

OPEN_CONNECTIONS = Gauge(
    "realtime_pubsub_open_connections",
    "Live WebSocket connections",
)

SUBSCRIPTIONS = Gauge(
    "realtime_pubsub_subscriptions_total",
    "Active topic subscriptions across all connections",
)

TOPICS = Gauge(
    "realtime_pubsub_topics_total",
    "Distinct topics with at least one subscriber",
)

PRESENCE_MEMBERS = Gauge(
    "realtime_pubsub_presence_members",
    "Current room membership count",
)
