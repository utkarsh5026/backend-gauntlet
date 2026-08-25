"""A managed queue service — the SQS data plane and control plane (project 29).

Project 04 built a queue as a library over Postgres. This one builds a queue as a
*service*, where the consumer is a stranger across a socket: it can vanish
mid-message, delete work it no longer owns, or park ten thousand idle
connections on you. See `SPEC.md`.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
