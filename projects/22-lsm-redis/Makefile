# lsm-redis — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # sync + reference redis + run your server
#   make ping      # redis-cli PING/SET/GET against your server (V1)
#   make data      # what's actually on disk: WAL bytes + SSTable count (V2/V4)
#   make crash     # kill -9 the server, then `make run` — did the write survive? (V2)
#   make bench     # redis-benchmark — the boss fight's load generator
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, docker, and (for smoke) curl. `redis-cli` is used if
# installed, otherwise the one in the compose container.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync up deps down ps logs \
         check lint fmt fmt-check types test verify clean run dev smoke \
         ping cli ref data stats metrics crash bench profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
