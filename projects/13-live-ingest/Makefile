# live-ingest — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make            # help
#   make run        # start the server (loads .env) — HTTP :8080 + RTMP :1935
#   make publish    # push a synthetic live stream at the RTMP port (ffmpeg)
#   make status     # sessions, streams, and the last session failure
#   make smoke-rtmp # end-to-end RTMP ingest test (needs ffmpeg)
#   make dev        # server + the web/ LL-HLS player
#   make verify     # fmt-check + lint + types + test
#
# Requires: python3, uv, and (for publish/smoke-rtmp) ffmpeg. No docker-compose
# here — the source is a live socket, everything else is in-process.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync check lint fmt fmt-check types test verify clean \
         run dev web-install frontend smoke smoke-rtmp publish \
         planes status streams playlist metrics bench profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
