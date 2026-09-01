# bittorrent — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make            # help
#   make dev        # sync + test swarm + run your client
#   make swarm      # who's up: tracker, reference peer, your ports
#   make torrent    # build a real .torrent (the V1/V2 corpus)
#   make add        # POST it at your control plane (V2)
#   make announce   # ask the compose tracker for peers (V3, over HTTP)
#   make handshake  # send the 68 bytes to your seeder (V4/V6)
#   make verify     # fmt-check + lint + types + test
#
# Requires: python3, uv, docker, and (for the probes) curl.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync up deps down ps logs \
         check lint fmt fmt-check types test verify clean run dev smoke \
         swarm announce torrent add torrents handshake metrics data bench profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
