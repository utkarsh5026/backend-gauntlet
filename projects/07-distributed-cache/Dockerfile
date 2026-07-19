# Multi-stage build for one cache node. The docker-compose file runs three of
# these to give you a real cluster to watch gossip + rebalancing on.
FROM rust:1-slim AS build
WORKDIR /app
# Build against the whole workspace so member `{ workspace = true }` deps resolve.
COPY . .
RUN cargo build --release -p distributed-cache

FROM debian:bookworm-slim AS runtime
# curl is only here for the compose healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/target/release/distributed-cache /usr/local/bin/distributed-cache
EXPOSE 8070/tcp 7070/udp
ENTRYPOINT ["distributed-cache"]
