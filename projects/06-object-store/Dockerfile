# Multi-stage image for both Rust binaries:
#   object-store         — S3 path-style API (blobs)
#   object-store-index   — metadata microservice
#
# Compose builds once and runs two services with different commands.
# Context must be the workspace root (see docker-compose.yml).
# Pinned to bookworm so the glibc in the build stage matches runtime
# (same pattern as projects/04-job-queue/Dockerfile).

FROM rust:1-slim-bookworm AS build
WORKDIR /app
COPY . .
RUN cargo build --release -p object-store \
      --bin object-store \
      --bin object-store-index

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/target/release/object-store /usr/local/bin/object-store
COPY --from=build /app/target/release/object-store-index /usr/local/bin/object-store-index
# Default command is the S3 front-end; compose overrides for the index service.
EXPOSE 9000/tcp 9106/tcp
ENTRYPOINT ["object-store"]
