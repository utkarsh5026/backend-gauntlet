# sqs-queue — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic. Run from this directory:
#
#   make           # help
#   make dev       # sync + run the broker
#   make verify    # fmt-check + lint + types + test
#
# No docker dependencies — the queue store is in memory, because durability is
# projects 04 and 08 and replication is 07 and 09. The only optional external
# piece is project 25's authorizer on :9026, once REQUIRE_SIGV4 is on.
# Requires: python3, uv, and (for the poke targets) curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync \
         check lint fmt fmt-check types test verify clean \
         run dev smoke create-queue send receive profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
