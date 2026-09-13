# Multi-stage build for one regional SFU of the cascade.
#
# Container-only checks this earns you, and the reason SPEC.md asks for it:
# uvloop is the loop here and it is *not* the loop pytest runs on. Both UDP
# planes are written on `create_datagram_endpoint` precisely because uvloop has
# no `loop.sock_*` family — a raw-socket backbone pump passes every test in
# `tests/` and raises `NotImplementedError` right here, on the first relay copy.
# PID 1 signal handling is real too, so `docker stop` proves the lifespan runs
# its ordered teardown (step down, tear down legs, finalize recordings) rather
# than being killed at the grace-period mark.
#
# Both stages share a base image so the venv built in `build` has valid
# interpreter paths in `runtime` — a venv is not relocatable across Pythons.
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Build against the whole uv workspace so the `common-*` workspace deps resolve.
# Manifests first: this layer only busts when a dependency actually changes.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY projects/17-global-conferencing/pyproject.toml projects/17-global-conferencing/
RUN uv sync --package global-conferencing --frozen --no-dev --no-install-project

COPY projects/17-global-conferencing/ projects/17-global-conferencing/
RUN uv sync --package global-conferencing --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for a healthcheck / `docker exec` probe against /healthz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 RECORDING_DIR=/data/recordings

# Recordings (V4) are durable output — mount a volume or they die with the container.
VOLUME ["/data/recordings"]

# Signaling + cluster control + admin, the participant media port, and the
# backbone. Both UDP ports must be published as **UDP**. PUBLIC_IP must be an
# address a *client* can reach, and every PEERS entry must name addresses the
# *other SFUs* can reach — inside a container, neither is the bind address.
#   docker run -p 8080:8080 -p 7000:7000/udp -p 7100:7100/udp \
#     -e REGION=eu-west -e PUBLIC_IP=<host-ip> -e PEERS=... global-conferencing
EXPOSE 8080/tcp
EXPOSE 7000/udp
EXPOSE 7100/udp

# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM directly
# and the lifespan teardown actually runs. Wrap it in `sh -c` and a leader never
# steps down, its legs stay half-open, and every open recording loses its index.
ENTRYPOINT ["global-conferencing"]
