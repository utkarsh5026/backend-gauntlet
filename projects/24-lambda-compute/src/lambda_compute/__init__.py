"""Project 24 — a serverless compute plane built from the Runtime API up.

See `SPEC.md`. The verticals live in `runtime_api.py` (V1), `environments.py`
(V2), `sandbox.py` (V3), `concurrency.py` (V4), `async_invoke.py` (V5) and
`event_source.py` (V6); everything else is wiring. Multi-node placement, VPC
attachment and IAM are deliberately out of scope — IAM is project 25.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
