# Multi-stage build for the live ingest server.
#
# Container-only checks this earns you, and the reason SPEC.md's Definition of
# done asks for it: uvloop is the loop here and it is *not* the loop pytest runs
# on. The RTMP listener is built on `asyncio.start_server` precisely because
# uvloop has no `loop.sock_*` family, and this is where that claim gets tested —
# a raw-socket accept loop passes every test in `tests/` and raises
# `NotImplementedError` right here, on the first broadcaster. PID 1 signal
# handling is real too, so `docker stop` proves uvicorn drains held blocking
# reloads and the lifespan stops the RTMP server rather than being killed at the
# ten-second mark.
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
COPY projects/13-live-ingest/pyproject.toml projects/13-live-ingest/
RUN uv sync --package live-ingest --frozen --no-dev --no-install-project

COPY projects/13-live-ingest/src/ projects/13-live-ingest/src/
RUN uv sync --package live-ingest --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for a healthcheck / `docker exec` probe against /healthz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# The delivery plane (HTTP) and the ingest plane (RTMP). Both TCP.
#   docker run -p 8080:8080 -p 1935:1935 -e STREAM_KEYS=testkey live-ingest
EXPOSE 8080/tcp
EXPOSE 1935/tcp

# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM directly
# from `docker stop` and its graceful shutdown actually runs. Wrap this in
# `sh -c` and the signal goes to the shell instead, held reloads are cut, and the
# RTMP sessions never see their teardown.
ENTRYPOINT ["live-ingest"]
