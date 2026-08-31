# url-shortener — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # sync + deps + migrate + run
#   make demo      # deps + migrate + serve, then open the browser dashboard
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, docker, and (for the bench harness) node.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync deps up down ps logs wait-db migrate reset-db \
         wait-redis check lint fmt fmt-check types test verify clean \
         run dev demo smoke bench bench-seed bench-smoke bench-load profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
