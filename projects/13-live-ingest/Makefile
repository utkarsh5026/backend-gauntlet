# live-ingest — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make            # help
#   make run        # start the server (loads .env)
#   make verify     # fmt-check + clippy + check + test
#   make smoke-rtmp # end-to-end RTMP ingest test (needs ffmpeg)
#
# Requires: python3, cargo, and (for smoke-rtmp) ffmpeg. There is no docker/DB here.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup check clippy fmt fmt-check test verify clean \
         run dev web-install frontend smoke smoke-rtmp md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
