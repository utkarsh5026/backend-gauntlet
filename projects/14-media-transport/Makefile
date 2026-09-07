# media-transport — local dev shortcuts
#
# This Makefile is a thin wrapper around `makefile.py`, which holds the real
# task logic (colors, emojis, readable output). Run from this directory:
#
#   make            # help
#   make run        # start one side (ROLE from .env)
#   make pair       # a receiver and a sender pointed at each other on localhost
#   make planes     # who's up: the admin plane, the RTP UDP port
#   make send       # send a real RTP datagram at the media port (V1)
#   make status     # role, bounds, and whether the session is still alive
#   make netem      # the tc impairments the boss fight runs behind
#   make verify     # fmt-check + lint + types + test
#
# Requires: python3, uv, and (for the probes) curl. No docker-compose here —
# there is no database and no broker; everything lives in-process.

PY := python3
RUNNER := $(PY) $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/makefile.py)

# Every known task. Keep in sync with the @task registry in makefile.py.
TASKS := help setup sync check lint fmt fmt-check types test verify clean \
         run pair smoke planes send status metrics \
         netem netem-off bench profile md

.DEFAULT_GOAL := help
.PHONY: $(TASKS)

$(TASKS):
	@$(RUNNER) $@
