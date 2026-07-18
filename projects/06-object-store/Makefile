# object-store — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # launch backend + web console together (the full demo)
#   make backend   # run just the Rust store
#   make frontend  # run just the web console
#   make verify    # fmt-check + clippy + check + test
#
# There is no Docker here — the filesystem is the store. Requires: python3,
# cargo, and (for the console) bun.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup web-install \
         check clippy fmt fmt-check test verify clean \
         dev backend frontend web-build smoke md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
