# Multi-stage build for the live platform.
#
# This image is what the k8s Deployments in `k8s/` run, and it earns two checks
# `make verify` cannot make. uvloop is the loop here, and it is *not* the loop
# pytest runs on — this is the first place the app runs on the loop production
# uses. And PID 1 signal handling is real: a rolling deploy sends SIGTERM, and
# `docker stop` proves the lifespan drains and closes every connection rather
# than being SIGKILLed at the grace-period boundary.
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
COPY projects/16-live-platform/pyproject.toml projects/16-live-platform/
RUN uv sync --package live-platform --frozen --no-dev --no-install-project

# The project directory includes `migrations/`, which `python -m
# live_platform.migrate` finds relative to the package — so the same image can run
# as a migration Job before the Deployment rolls.
COPY projects/16-live-platform/ projects/16-live-platform/
RUN uv sync --package live-platform --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for a healthcheck / `docker exec` probe against /healthz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

EXPOSE 8080

# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM directly
# and its graceful shutdown actually runs. Wrap this in `sh -c` and the signal goes
# to the shell instead: held playlist reloads are cut, NATS is never drained, and
# the pod is SIGKILLed at the end of its grace period.
ENTRYPOINT ["live-platform"]
