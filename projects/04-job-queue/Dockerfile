# Multi-stage build for the job-queue app itself, so `docker compose up` can
# run it alongside Postgres for a container-parity check. `make dev` still
# runs the binary on the host via `cargo run` — that stays the fast edit/
# compile loop while you're filling in the `todo!()`s; this image is for
# exercising the real deployed shape (env vars only, no host cargo toolchain).
# Pinned to the same Debian release as the runtime stage (`rust:1-slim` alone
# floats onto whatever Debian is current — e.g. trixie — and its newer glibc
# won't run on a bookworm runtime: `GLIBC_2.39' not found).
FROM rust:1-slim-bookworm AS build
WORKDIR /app
# Build against the whole workspace so member `{ workspace = true }` deps resolve.
COPY . .
RUN cargo build --release -p job-queue

FROM debian:bookworm-slim AS runtime
# curl is only here for the compose healthcheck (GET /healthz).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/target/release/job-queue /usr/local/bin/job-queue
EXPOSE 8080/tcp
ENTRYPOINT ["job-queue"]
