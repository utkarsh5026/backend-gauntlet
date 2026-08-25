# Multi-stage build for the broker. There are no service dependencies — the
# filesystem is the log — so the only thing worth mounting is DATA_DIR.
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
COPY projects/08-message-broker/pyproject.toml projects/08-message-broker/
RUN uv sync --package message-broker --frozen --no-dev --no-install-project

COPY projects/08-message-broker/ projects/08-message-broker/
RUN uv sync --package message-broker --frozen --no-dev

FROM python:3.14-slim-bookworm AS runtime
# curl is only here for the container healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
# The log outlives the container: mount a volume here or every restart is a
# fresh broker, which makes V1's recovery path untestable in Docker.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 9092/tcp
ENTRYPOINT ["message-broker"]
