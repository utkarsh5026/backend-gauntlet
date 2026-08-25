"""Project 25 — IAM + STS, built from the signature up.

See `SPEC.md`. The verticals live in `sigv4.py` (V1), `policy.py` (V2),
`evaluation.py` (V3), `sts.py` (V4), `authorizer.py` (V5) and `audit.py` (V6);
everything else is wiring.

This is the security horizontal for Tier 8: its payoff is project 23, 24 or 06
calling this service's authorization endpoint and being correctly denied.
Federation, the console and key management are out of scope — KMS is project 28.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
