# vod-streaming — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make           # help
#   make fixture   # generate a two-rendition test asset (needs ffmpeg)
#   make run       # serve it
#   make playlist  # what V3 produces
#   make range     # what V4 answers to a Range request
#   make validate  # feed init+seg to ffprobe — V2's Proof
#   make verify    # fmt-check + lint + types + test
#
# Requires: python3, uv, and (for fixture/validate) ffmpeg.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync web-install \
         check lint fmt fmt-check types test verify clean \
         run dev frontend smoke docker fixture \
         assets playlist range validate metrics profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
