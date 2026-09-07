# object-store — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # backend + web console together (the full demo)
#   make backend   # run just the store
#   make verify    # fmt-check + lint + types + test — what CI runs
#   make layout    # what is actually on disk under DATA_DIR
#   make dedup     # logical bytes vs physical bytes — the V1 payoff
#
# The filesystem is the store, so there is no database to start. The compose
# stack exists only for the From-the-field index split. Requires: python3, uv,
# and (for the console) bun.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync web-install \
         check lint fmt fmt-check types test verify clean \
         run dev backend frontend index-svc web-build smoke md \
         up down ps logs deps stack \
         layout dedup gc stats reset \
         bench bench-tier bench-haystack profile

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
