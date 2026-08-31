# Multi-stage build for the job-queue app, so `docker compose up` can run it
# alongside Postgres for a container-parity check. `make dev` still runs the
# server on the host — that stays the fast edit loop; this image is for
# exercising the real deployed shape (env vars only, no host toolchain).
#
# Container-only checks this earns you: uvloop is the loop here (it is not
# always the one pytest uses), and PID 1 signal handling is real, so a
# `docker stop` proves the lifespan actually drains the worker pool.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# The whole workspace, because `common-config`/`common-telemetry` are workspace
# members and the single root uv.lock pins every version.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY projects/04-job-queue/pyproject.toml projects/04-job-queue/
RUN uv sync --frozen --no-dev --package job-queue --no-install-project

COPY projects/04-job-queue/ projects/04-job-queue/
RUN uv sync --frozen --no-dev --package job-queue

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for the compose healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Don't run as root: the exec/shell job kinds run whatever a job asks for.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER app
WORKDIR /app/projects/04-job-queue
EXPOSE 8080/tcp
ENTRYPOINT ["job-queue"]
