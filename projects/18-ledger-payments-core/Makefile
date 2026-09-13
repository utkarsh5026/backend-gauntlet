# ledger-payments-core — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make              # help
#   make dev          # postgres + redis up → migrate → run the API
#   make account      # POST /accounts            (V1)
#   make transfer     # POST /transfers            (V2, + KEY= for V3)
#   make audit        # Σ entries over the whole ledger — must be 0
#   make receiver     # a local webhook sink on :9000 (FAIL=1 to watch the DLQ)
#   make dispatcher   # run with the webhook dispatcher on (V4)
#   make verify       # fmt-check + lint + types + test
#
# Requires: python3, uv, docker compose.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync check lint fmt fmt-check types test verify clean \
         up deps down ps logs migrate reset-db psql redis-cli \
         run dispatcher dev smoke account transfer balance txn audit metrics receiver \
         bench profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
