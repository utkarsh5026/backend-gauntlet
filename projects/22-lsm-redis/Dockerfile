# Multi-stage build for the LSM engine + RESP server.
#
# Container-only checks this earns you, and the reason SPEC.md's Definition of
# done asks for it: uvloop is the loop here and it is *not* the loop pytest runs
# on, and PID 1 signal handling is real — so a `docker stop` proves the lifespan
# actually drains RESP connections and fsyncs the WAL rather than losing the last
# writes at the 10-second kill.
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
COPY projects/22-lsm-redis/pyproject.toml projects/22-lsm-redis/
RUN uv sync --package lsm-redis --frozen --no-dev --no-install-project

COPY projects/22-lsm-redis/ projects/22-lsm-redis/
RUN uv sync --package lsm-redis --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for the compose healthcheck (GET /healthz on the sidecar).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# The data directory IS the database. Declaring it a volume means a container
# restart keeps the store — which is the only way to test WAL replay and SSTable
# reopening the way they will actually be exercised.
ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 6379/tcp
EXPOSE 8080/tcp
# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM directly
# from `docker stop` and its graceful shutdown actually runs. Wrap this in
# `sh -c` and the signal goes to the shell instead, the lifespan's `finally`
# never runs, and the WAL is never flushed — which is a data-loss bug introduced
# by a Dockerfile.
ENTRYPOINT ["lsm-redis"]
