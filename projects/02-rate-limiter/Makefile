# rate-limiter — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # deps + run
#   make verify    # fmt-check + clippy + check + test
#
# Requires: python3, docker, cargo, and (for smoke) grpcurl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup deps up down ps logs wait-redis reset \
         check clippy fmt fmt-check test verify clean run dev smoke md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
