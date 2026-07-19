# Multi-stage build for the gateway. docker-compose runs one of these in front of
# a pool of echo backends so you can watch load balancing + failover.
FROM rust:1-slim AS build
WORKDIR /app
# Build against the whole workspace so member `{ workspace = true }` deps resolve.
COPY . .
RUN cargo build --release -p api-gateway

FROM debian:bookworm-slim AS runtime
# curl is only here for the compose healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/target/release/api-gateway /usr/local/bin/api-gateway
EXPOSE 8080/tcp
ENTRYPOINT ["api-gateway"]
