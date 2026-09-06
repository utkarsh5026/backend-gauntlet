# Multi-stage build for the WebRTC SFU.
#
# Container-only checks this earns you, and the reason SPEC.md's Definition of
# done asks for it: uvloop is the loop here and it is *not* the loop pytest runs
# on. The media plane is written on `create_datagram_endpoint` precisely because
# uvloop has no `loop.sock_*` family, and this is where that claim gets tested —
# a raw-socket pump passes every test in `tests/` and raises `NotImplementedError`
# right here, on the first datagram. PID 1 signal handling is real too, so
# `docker stop` proves the lifespan actually stops forwarding and closes the
# media socket rather than being killed at the ten-second mark.
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
COPY projects/15-webrtc-sfu/pyproject.toml projects/15-webrtc-sfu/
RUN uv sync --package webrtc-sfu --frozen --no-dev --no-install-project

COPY projects/15-webrtc-sfu/ projects/15-webrtc-sfu/
RUN uv sync --package webrtc-sfu --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# curl is only here for a healthcheck / `docker exec` probe against /healthz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# The signaling + admin plane, and the muxed media port. The media port must be
# published as **UDP** and be reachable, because it is the address handed out as
# the ICE host candidate — a browser that cannot send a STUN check to it never
# connects, and the failure is silent on both sides. That is also why PUBLIC_IP
# has to be set to an address a *client* can reach: inside a container the bind
# address and the reachable address are never the same thing, and advertising
# the wrong one looks exactly like a broken ICE implementation.
#   docker run -p 8080:8080 -p 7000:7000/udp -e PUBLIC_IP=<host-ip> webrtc-sfu
EXPOSE 8080/tcp
EXPOSE 7000/udp

# No shell wrapper: the entrypoint is PID 1, so uvicorn receives SIGTERM
# directly from `docker stop` and its graceful shutdown actually runs. Wrap this
# in `sh -c` and the signal goes to the shell instead, the lifespan's `finally`
# never runs, the media socket is never closed, and peers keep sending at a
# process that has stopped forwarding — a session-visible bug introduced by a
# Dockerfile.
ENTRYPOINT ["webrtc-sfu"]
