# workflow-engine — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # deps + migrate + run
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, docker, and (for smoke/health) grpcurl and curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync proto up down ps logs deps wait-db migrate reset-db db-ui \
         check lint fmt fmt-check types test verify clean \
         run dev dev-container smoke health profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
