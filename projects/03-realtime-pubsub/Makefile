# realtime-pubsub — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make dev       # full stack: Redis + server + web playground (mprocs)
#   make frontend  # Vite playground only (:5173)
#   make run       # single-node server (V1–V3; no Redis needed)
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, docker, mprocs (for make dev), bun (for web/).

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync web-install up deps db down ps logs wait-redis \
         obs-up obs-down \
         check lint fmt fmt-check types test verify clean \
         run dev frontend ws smoke profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
