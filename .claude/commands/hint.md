---
description: Give a graduated hint on a stuck SPEC challenge — never the full solution
argument-hint: <what you're stuck on, e.g. "V2 stampede protection in project 01">
---

The user is stuck on: **$ARGUMENTS**

This is a LEARNING repo (see CLAUDE.md). Help them get unstuck WITHOUT solving it.

1. Find the relevant project's `SPEC.md` and the `todo!()`/code in question; read
   what the user has written so far.
2. Give a **graduated hint**, smallest first. Offer ONE level at a time and ask if
   they want to go deeper:
   - **L1 — Reframe:** restate the core problem and the key insight/property needed.
   - **L2 — Direction:** name the technique/data structure/crate concept to research
     (e.g. "single-flight", "SET NX PX", "CAS loop", "QueryBuilder::push_values").
   - **L3 — Shape:** describe the algorithm in steps or pseudocode — still their code
     to write.
3. Do **not** write the actual solution unless they explicitly ask after L3.
4. If they have a bug, point at the *symptom and where to look*, not the fix.

End by asking whether they want the next hint level or to try it themselves.
