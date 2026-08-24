# rate-limiter — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # sync + Redis + run
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, docker, and (for smoke/health) grpcurl and curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync proto deps up down ps logs wait-redis reset \
         check lint fmt fmt-check types test verify clean \
         run dev smoke health profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
