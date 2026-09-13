"""Ledger / payments core (Stripe-lite) — project 18 of the backend gauntlet.

Moving money is the one place in backend where a race condition has a dollar figure
attached. An append-only double-entry ledger (V1), transfers that hold the
no-overdraft invariant under any concurrency (V2), idempotency keys so a client's
retry can't double-charge (V3), and signed webhooks delivered through a
transactional outbox (V4). See `SPEC.md`, and `main` for how the pieces are wired.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
