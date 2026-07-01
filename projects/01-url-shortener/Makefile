# url-shortener — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # deps + migrate + run
#   make demo      # deps + migrate + serve, then open the browser dashboard
#   make verify    # fmt-check + clippy + check + test
#
# Requires: python3, docker, cargo, and (for migrate) sqlx-cli.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup install-tools deps up down ps logs wait-db migrate reset-db \
         check clippy fmt fmt-check test verify clean run dev demo smoke \
         bench bench-seed bench-smoke bench-load

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
