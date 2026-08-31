"""A durable workflow engine — Temporal, rebuilt small enough to understand.

The promise is **durable execution**: you write a normal-looking function
("charge the card, wait 3 days, if not cancelled ship the order"), and it runs to
completion exactly as written even though the process running it will crash,
deploy and restart many times before it finishes. The only way to keep that
promise is to stop storing the program's *state* and start storing its
*history* — an append-only log from which state can be replayed on any machine at
any time. See `SPEC.md`; the five verticals are `history`, `replay`, `timers`,
`dispatch` and `sticky`.
"""
