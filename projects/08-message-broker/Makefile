# message-broker — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # sync + run the broker
#   make walk      # produce/fetch walkthrough against a running broker
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, and (for smoke/walk) curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync \
         check lint fmt fmt-check types test verify clean \
         run dev smoke walk tree wipe profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
