# Multi-stage image for both entry points:
#   object-store         — the S3 path-style API (blobs)
#   object-store-index   — the metadata microservice
#
# Compose builds this once and runs two services off different entrypoints.
# The build context must be the workspace root (see docker-compose.yml), because
# the uv workspace's `packages/common-*` live above this directory.
#
# Container-only checks this earns you, and the reason SPEC.md's Definition of
# done asks for it: uvicorn runs on **uvloop** here and pytest does not, and
# PID-1 signal handling is real. A store's graceful shutdown is not decorative —
# a SIGTERM that kills the process outright can interrupt a blob commit between
# its rename and its directory fsync, which is exactly the window the whole
# durability dance exists to close.
#
# Both stages sit on the same base image so the virtualenv built in `build` has
# valid interpreter paths when copied into `runtime` — a venv is not relocatable
# across different Python installs.
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Manifests first: this layer only busts when a dependency actually changes, so
# an ordinary source edit re-runs neither the resolver nor the wheel downloads.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY projects/06-object-store/pyproject.toml projects/06-object-store/
RUN uv sync --package object-store --frozen --no-dev --no-install-project

COPY projects/06-object-store/ projects/06-object-store/
RUN uv sync --package object-store --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for a healthcheck / `docker exec` probe against /healthz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# The store *is* the filesystem, so this is the one thing that must be a volume.
# Without it every restart is a fresh, empty store, and the crash-consistency
# properties the project is about become untestable.
ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 9000/tcp 9106/tcp

# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM
# directly from `docker stop` and its graceful shutdown actually runs. Wrap this
# in `sh -c` and the signal goes to the shell instead, the lifespan's `finally`
# never runs, the background sweepers are killed mid-pass, and in-flight
# transfers are cut rather than drained.
ENTRYPOINT ["object-store"]
