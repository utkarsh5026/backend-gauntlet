# Multi-stage build for the url-shortener service.
#
# Build from the **repo root** so the uv workspace (and the `common-*` packages
# this project depends on) are in the context:
#
#   docker build -f projects/01-url-shortener/Dockerfile -t url-shortener .
#
# Both stages sit on the same base image so the virtualenv built in `build` has
# valid interpreter paths when copied into `runtime` — a venv is not relocatable
# across different Python installs.
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Manifests first: this layer only busts when a dependency actually changes.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY projects/01-url-shortener/pyproject.toml projects/01-url-shortener/
RUN uv sync --package url-shortener --frozen --no-dev --no-install-project

COPY projects/01-url-shortener/ projects/01-url-shortener/
RUN uv sync --package url-shortener --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for the compose healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
# Put the venv first so `url-shortener` resolves to the console script.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# The demo dashboard is deliberately absent: the repo's .dockerignore excludes
# `**/dist/`, so the built SPA never enters the build context and `GET /` answers
# 404 here. That is the intended split — the container is the service, the
# dashboard is a local demo you run with `make demo`. The API, /healthz and
# /metrics are complete.

EXPOSE 8080/tcp
# Migrations are not run on boot. Schema changes are an operator action with
# their own failure modes; a replica that migrates on startup turns a rollout
# into N concurrent migrations racing each other.
ENTRYPOINT ["url-shortener"]
