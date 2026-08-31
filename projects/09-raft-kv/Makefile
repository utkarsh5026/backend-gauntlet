# raft-kv — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # sync + run the whole cluster
#   make cluster   # every node in PEERS, one terminal
#   make watch     # poll /status on every node
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, and (for smoke/walk/watch) curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync \
         check lint fmt fmt-check types test verify clean \
         run dev cluster watch walk smoke wipe profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
