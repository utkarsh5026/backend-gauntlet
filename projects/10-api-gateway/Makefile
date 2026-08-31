# api-gateway — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # sync + backend pool + run the gateway locally
#   make spread    # see which backend served each request (V3)
#   make headers   # see what the backend received (V1)
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, docker, and (for smoke/probes) curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync up deps down ps logs wait-backends kill-backend revive-backend \
         check lint fmt fmt-check types test verify clean run dev demo smoke \
         routes spread headers metrics profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
