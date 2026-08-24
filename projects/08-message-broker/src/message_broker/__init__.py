"""Project 08 — a Kafka-lite message broker built from the log up.

See `SPEC.md`. The verticals live in `log.py` (V1), `index.py` (V2), `topic.py`
(V3) and `group.py` (V4); everything else — `broker.py`, `partition.py`,
`record.py`, `routes.py`, `main.py` — is wiring.

There is no external dependency: the filesystem IS the broker.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
