# Multi-stage build for the media transport.
#
# Container-only checks this earns you, and the reason SPEC.md's Definition of
# done asks for it: uvloop is the loop here and it is *not* the loop pytest runs
# on. The media plane is written on `create_datagram_endpoint` precisely because
# uvloop has no `loop.sock_*` family, and this is where that claim gets tested —
# a raw-socket receive loop passes every test in `tests/` and raises
# `NotImplementedError` right here, on the first datagram. PID 1 signal handling
# is real too, so `docker stop` proves the lifespan actually stops the session
# and closes the socket rather than being killed at the ten-second mark.
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
COPY projects/14-media-transport/pyproject.toml projects/14-media-transport/
RUN uv sync --package media-transport --frozen --no-dev --no-install-project

COPY projects/14-media-transport/ projects/14-media-transport/
RUN uv sync --package media-transport --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for a healthcheck / `docker exec` probe against /healthz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# The admin plane, and the media port. The media port must be published as
# **UDP** — publishing it as TCP looks identical in `docker ps` and delivers
# exactly nothing, which is a confusing hour to spend.
#   docker run -p 8080:8080 -p 5004:5004/udp media-transport
EXPOSE 8080/tcp
EXPOSE 5004/udp

# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM
# directly from `docker stop` and its graceful shutdown actually runs. Wrap this
# in `sh -c` and the signal goes to the shell instead, the lifespan's `finally`
# never runs, the socket is never closed, and the peer keeps sending at a
# process that has stopped receiving.
ENTRYPOINT ["media-transport"]
