# Multi-stage build for one Raft node. There are no service dependencies — the
# filesystem is the durable state and the peers are more copies of this image —
# so the only thing worth mounting is DATA_DIR.
#
# Container-only checks this earns you: uvloop is the loop here (it is not the
# one pytest uses), and PID 1 signal handling is real, so a `docker stop` proves
# the lifespan actually cancels the driver and flushes the persistent state.
#
# Both stages sit on the same base image so the virtualenv built in `build` has
# valid interpreter paths when copied into `runtime` — a venv is not relocatable
# across different Python installs.
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Build against the whole uv workspace so the `common-*` workspace deps resolve.
# Manifests first: this layer only busts when a dependency actually changes.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY projects/09-raft-kv/pyproject.toml projects/09-raft-kv/
RUN uv sync --package raft-kv --frozen --no-dev --no-install-project

COPY projects/09-raft-kv/ projects/09-raft-kv/
RUN uv sync --package raft-kv --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for the container healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
# The persistent state outlives the container: mount a volume here, or every
# restart is a node with no memory of its term, vote or log — which makes V1's
# "a vote survives a restart" criterion untestable in Docker.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 DATA_DIR=/data
VOLUME ["/data"]
# Node 1's port in the single-node default PEERS. A real cluster passes its own
# NODE_ID and PEERS, and publishes each node's port from that map.
EXPOSE 9001/tcp
ENTRYPOINT ["raft-kv"]
