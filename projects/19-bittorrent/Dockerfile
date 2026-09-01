# Multi-stage build for the BitTorrent client + seeder.
#
# Container-only checks this earns you, and the reason SPEC.md's checklist asks
# for it: uvloop is the loop here and it is *not* the loop pytest runs on — the
# UDP tracker (V3) is written against `create_datagram_endpoint` precisely
# because uvloop has no `loop.sock_*` family, and this is where that claim gets
# tested. PID 1 signal handling is real too, so `docker stop` proves the
# lifespan actually drains peer sessions and announces `stopped` rather than
# leaving a ghost in the swarm at the ten-second kill.
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
COPY projects/19-bittorrent/pyproject.toml projects/19-bittorrent/
RUN uv sync --package bittorrent --frozen --no-dev --no-install-project

COPY projects/19-bittorrent/ projects/19-bittorrent/
RUN uv sync --package bittorrent --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for the compose healthcheck (GET /healthz on the control plane).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# Piece data. A volume because a restart that keeps the directory is the only
# way to exercise resume (V5) the way it will actually be used — the client
# re-hashes what is there rather than refetching it.
ENV DOWNLOAD_DIR=/data
VOLUME ["/data"]

# The control plane, and the port peers dial us on. The peer port must be
# publishable and reachable for anyone to connect *in* — a seeder behind an
# unmapped port is a client that can only ever leech, which is a NAT problem
# wearing a Dockerfile.
EXPOSE 8080/tcp
EXPOSE 6819/tcp
EXPOSE 6819/udp

# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM
# directly from `docker stop` and its graceful shutdown actually runs. Wrap this
# in `sh -c` and the signal goes to the shell instead, the lifespan's `finally`
# never runs, no `stopped` is announced, and you linger in the swarm as a peer
# that answers nothing — a swarm-visible bug introduced by a Dockerfile.
ENTRYPOINT ["bittorrent"]
