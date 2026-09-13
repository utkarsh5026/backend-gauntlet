# Multi-stage build for the transcode pipeline.
#
# This image earns two checks `make verify` cannot make. uvloop is the loop here,
# and it is *not* the loop pytest runs on — this is the first place the workers'
# ffmpeg subprocesses are spawned and reaped on the loop production uses. And PID 1
# signal handling is real: `docker stop` sends SIGTERM, and the lifespan has to stop
# claiming, drain in-flight tasks and close the pool before the grace period ends.
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
COPY projects/12-transcode-pipeline/pyproject.toml projects/12-transcode-pipeline/
RUN uv sync --package transcode-pipeline --frozen --no-dev --no-install-project

# The project directory includes `migrations/`, which `python -m
# transcode_pipeline.migrate` finds relative to the package — so the same image
# can migrate the database before the workers start.
COPY projects/12-transcode-pipeline/ projects/12-transcode-pipeline/
RUN uv sync --package transcode-pipeline --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# ffmpeg/ffprobe are the one external toolbox this service shells out to: a worker
# image without them boots fine and fails its first Split. curl is only here for a
# healthcheck / `docker exec` probe against /healthz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

EXPOSE 8080

# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM directly
# and the lifespan's drain actually runs. Wrap this in `sh -c` and the signal goes
# to the shell instead: workers keep claiming, in-flight encodes are SIGKILLed at
# the grace-period boundary, and every task they held waits out its full lease.
ENTRYPOINT ["transcode-pipeline"]
