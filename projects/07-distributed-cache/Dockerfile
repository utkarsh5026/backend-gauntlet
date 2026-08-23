# Multi-stage build for one cache node. The docker-compose file runs three of
# these to give you a real cluster to watch gossip + rebalancing on.
#
# Both stages sit on the same base image so the virtualenv built in `build` has
# valid interpreter paths when copied into `runtime` — a venv is not relocatable
# across different Python installs.
FROM python:3.14-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Build against the whole uv workspace so the `common-*` workspace deps resolve.
# Manifests first: this layer only busts when a dependency actually changes.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY projects/07-distributed-cache/pyproject.toml projects/07-distributed-cache/
RUN uv sync --package distributed-cache --frozen --no-dev --no-install-project

COPY projects/07-distributed-cache/ projects/07-distributed-cache/
RUN uv sync --package distributed-cache --frozen --no-dev

FROM python:3.14-slim-bookworm AS runtime
# curl is only here for the compose healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
# Put the venv first so `distributed-cache` resolves to the console script.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8070/tcp 7070/udp
ENTRYPOINT ["distributed-cache"]
