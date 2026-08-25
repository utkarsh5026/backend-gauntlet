# iam-sts — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic. Run from this directory:
#
#   make           # help
#   make dev       # sync + run the node
#   make verify    # fmt-check + lint + types + test
#
# No docker dependencies — the identity store is in memory (durability and
# replication are projects 09 and 07). The external pieces are the projects this
# one protects: point 23, 24 or 06 at the authorizer on :9026.
# Requires: python3, uv, and (for smoke/unsigned/authorize) curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync \
         check lint fmt fmt-check types test verify clean \
         run dev smoke unsigned whoami authorize profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
