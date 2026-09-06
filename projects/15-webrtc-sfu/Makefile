# webrtc-sfu — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make            # help
#   make dev        # sync + run the SFU + the browser playground
#   make planes     # who's up: signaling HTTP, the muxed media UDP port
#   make publish    # create a publisher with three simulcast layers (V3's input)
#   make sub        # attach a subscriber to it
#   make rooms      # the live topology
#   make stun       # send a real STUN Binding request at the media port (V1)
#   make verify     # fmt-check + lint + types + test
#
# Requires: python3, uv, and (for the probes) curl. No docker-compose here —
# the SFU *is* the media server, and everything lives in-process.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync check lint fmt fmt-check types test verify clean \
         run dev smoke web planes publish sub rooms stun status metrics \
         bench profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
