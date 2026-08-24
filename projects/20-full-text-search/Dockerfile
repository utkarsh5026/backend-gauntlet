# Single-stage-per-role build for the search engine.
#
# There is no docker-compose file for this project and there does not need to be:
# the engine depends on nothing but a filesystem. The container exists so the
# runtime that actually ships gets exercised — uvloop rather than the stdlib
# event loop, and PID 1 signal handling for the graceful-shutdown criterion,
# neither of which `make verify` can see.
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
COPY projects/20-full-text-search/pyproject.toml projects/20-full-text-search/
RUN uv sync --package full-text-search --frozen --no-dev --no-install-project

COPY projects/20-full-text-search/ projects/20-full-text-search/
RUN uv sync --package full-text-search --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for the healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
# Put the venv first so `full-text-search` resolves to the console script.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# The index is the filesystem, so this is the one path worth mounting a volume
# at — without one, every container restart starts from an empty corpus.
ENV INDEX_DIR=/data
VOLUME ["/data"]
EXPOSE 9200/tcp
ENTRYPOINT ["full-text-search"]
