# lambda-compute — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic. Run from this directory:
#
#   make           # help
#   make dev       # sync + run the node
#   make verify    # fmt-check + lint + types + test
#
# No docker dependencies — this project IS the compute plane. The only optional
# external piece is project 23, whose stream V6's poller reads.
# Requires: python3, uv, and (for smoke/register/invoke) curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync \
         check lint fmt fmt-check types test verify clean \
         run dev smoke register invoke profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
