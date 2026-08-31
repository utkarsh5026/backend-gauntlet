# Multi-stage build for the realtime pub/sub server. Built from the *repo root*
# so the uv workspace (and the shared `packages/common-*`) resolve:
#
#   docker build -f projects/03-realtime-pubsub/Dockerfile -t realtime-pubsub .
#
# Both stages sit on the same base image so the virtualenv built in `build` has
# valid interpreter paths when copied into `runtime` — a venv is not relocatable
# across different Python installs.
FROM python:3.14-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Manifests first: this layer only busts when a dependency actually changes.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY projects/03-realtime-pubsub/pyproject.toml projects/03-realtime-pubsub/
RUN uv sync --package realtime-pubsub --frozen --no-dev --no-install-project

COPY projects/03-realtime-pubsub/ projects/03-realtime-pubsub/
RUN uv sync --package realtime-pubsub --frozen --no-dev

FROM python:3.14-slim-bookworm AS runtime
# curl is only here for the compose/k8s healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
# Put the venv first so `realtime-pubsub` resolves to the console script.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8080/tcp
# No shell wrapper: the console script is PID 1, so SIGTERM reaches uvicorn
# directly and the lifespan gets to close live sockets with a proper close frame
# instead of the whole process being killed out from under them.
ENTRYPOINT ["realtime-pubsub"]
