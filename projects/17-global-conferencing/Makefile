# global-conferencing — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make            # help
#   make run        # one regional SFU (loads .env)
#   make mesh       # three regional SFUs on one host, one mprocs pane each
#   make planes     # who's up: HTTP, the media UDP port, the backbone UDP port
#   make publish    # POST a 3-layer publish — the first one places the room (V1)
#   make rooms      # the global topology this node agrees on
#   make vote       # poke /cluster/vote the way a peer would (V1)
#   make relay      # send a datagram at the backbone port (V2)
#   make verify     # fmt-check + lint + types + test
#
# Requires: python3, uv, and (for `make mesh`) mprocs. No docker-compose here —
# the control plane is consensus this process builds, not an external store.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync check lint fmt fmt-check types test verify clean \
         run dev web-install frontend smoke mesh planes publish sub rooms \
         status metrics vote relay bench profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
