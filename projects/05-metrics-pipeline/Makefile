# metrics-pipeline — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # sync + deps + run
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, docker, and (for send/stream/smoke) curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync up down ps logs schema \
         check lint fmt fmt-check types test verify clean \
         run dev send stream smoke profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
