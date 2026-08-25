# Single-stage-built, slim-shipped image for the rate limiter.
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
COPY projects/02-rate-limiter/pyproject.toml projects/02-rate-limiter/
RUN uv sync --package rate-limiter --frozen --no-dev --no-install-project

# The generated protobuf bindings are committed (see src/rate_limiter/pb), so
# there is no codegen step here — the image builds from source as it stands.
COPY projects/02-rate-limiter/ projects/02-rate-limiter/
RUN uv sync --package rate-limiter --frozen --no-dev

FROM python:3.14-slim-bookworm AS runtime
# curl is only here for the compose/orchestrator healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
# Put the venv first so `rate-limiter` resolves to the console script.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# gRPC, then the admin/metrics port.
EXPOSE 50051/tcp 9102/tcp
# Exec form, so the process is PID 1 and receives SIGTERM directly — which is
# what the graceful drain in main.py is listening for.
ENTRYPOINT ["rate-limiter"]
