# transcode-pipeline — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make            # help
#   make dev        # postgres up → migrate → run the control-plane API
#   make fixture    # generate a test source at $WORK_DIR/bbb.mp4 (needs ffmpeg)
#   make probe      # V1's input: the fixture's keyframes + duration
#   make submit     # POST /jobs for the fixture (V2)
#   make workers    # run with the scheduler + worker pool on
#   make verify     # fmt-check + lint + types + test
#
# Requires: python3, uv, docker compose, ffmpeg.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync check lint fmt fmt-check types test verify clean \
         up deps down ps logs migrate reset-db \
         run workers dev smoke fixture probe submit job metrics \
         bench profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
