# Multi-stage build for the VOD server.
#
# There is no docker-compose in this project — the filesystem is the source, so
# there is nothing to stand up alongside it. The image still matters, and the
# SPEC's Definition of done asks for it, because it is the only place two things
# are exercised that `make verify` cannot see: **uvloop** is the event loop here
# and is *not* the loop pytest runs on, and the entrypoint is **PID 1**, so a
# `docker stop` proves the graceful shutdown actually drains an in-flight segment
# read instead of truncating it mid-fragment.
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
COPY projects/11-vod-streaming/pyproject.toml projects/11-vod-streaming/
RUN uv sync --package vod-streaming --frozen --no-dev --no-install-project

COPY projects/11-vod-streaming/ projects/11-vod-streaming/
RUN uv sync --package vod-streaming --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is here to probe /healthz from inside the container when host port
# publishing is not available.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# The media library is data, not image content: mount a host directory here
# (`-v $PWD/media:/media`) rather than baking video into the image. Created so
# the server starts with an empty library instead of failing on a missing path.
ENV MEDIA_DIR=/media
RUN mkdir -p /media
VOLUME ["/media"]

EXPOSE 8080/tcp
# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM directly
# from `docker stop` and its graceful shutdown actually runs. Wrap this in
# `sh -c` and the signal goes to the shell instead, the drain never happens, and
# every in-flight segment download is truncated at the 10-second kill.
ENTRYPOINT ["vod-streaming"]
