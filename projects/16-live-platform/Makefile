# live-platform — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make            # help
#   make dev        # deps up → migrate → seed a demo key → run the server
#   make up         # postgres + redis + nats, waiting for their healthchecks
#   make ingest     # POST /ingest/start for the demo key (V1)
#   make play       # GET the demo stream's master playlist (V3)
#   make status     # the registry, the ladder, queue depth, chat channels
#   make verify     # fmt-check + lint + types + test
#
# Requires: python3, uv, docker compose.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync check lint fmt fmt-check types test verify clean \
         up deps down ps logs migrate seed reset-db \
         run dev smoke status metrics ingest play \
         bench profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
