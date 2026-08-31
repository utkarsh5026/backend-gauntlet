# Multi-stage build for the gateway. docker-compose runs one of these in front of
# a pool of echo backends so you can watch load balancing and failover.
#
# Container-only checks this earns you, and the reason SPEC.md's Definition of
# done asks for it: uvloop is the loop here and it is *not* the loop pytest runs
# on, and PID 1 signal handling is real, so a `docker stop` proves the lifespan
# actually cancels the health checker and drains in-flight proxied requests
# instead of truncating them.
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
COPY projects/10-api-gateway/pyproject.toml projects/10-api-gateway/
RUN uv sync --package api-gateway --frozen --no-dev --no-install-project

COPY projects/10-api-gateway/ projects/10-api-gateway/
RUN uv sync --package api-gateway --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for the compose healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8080/tcp
# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM directly
# from `docker stop` and its graceful shutdown actually runs. Wrap this in
# `sh -c` and the signal goes to the shell instead, the drain never happens, and
# every in-flight download is truncated at the 10-second kill.
ENTRYPOINT ["api-gateway"]
