# Built fat, shipped slim, for the workflow engine.
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
COPY projects/21-workflow-engine/pyproject.toml projects/21-workflow-engine/
RUN uv sync --package workflow-engine --frozen --no-dev --no-install-project

# The generated protobuf bindings are committed (see src/workflow_engine/pb), so
# there is no codegen step here — the image builds from source as it stands.
COPY projects/21-workflow-engine/ projects/21-workflow-engine/
RUN uv sync --package workflow-engine --frozen --no-dev

FROM python:3.14-slim-bookworm AS runtime
# curl is only here for the compose/orchestrator healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
# Put the venv first so `workflow-engine` resolves to the console script.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# gRPC, then the admin/metrics port.
EXPOSE 7233/tcp 9121/tcp
# Exec form, so the process is PID 1 and receives SIGTERM directly — which is
# what the graceful drain in main.py is listening for. That drain is the only
# thing standing between `docker stop` and a fleet of workers whose long-polls
# were cut off mid-claim.
ENTRYPOINT ["workflow-engine"]
