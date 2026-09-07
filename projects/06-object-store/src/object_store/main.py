"""S3-compatible object store — entrypoint and wiring.

The plumbing is wired for you: configuration, the on-disk layout, the FastAPI
app, `/metrics`, the three background loops, and graceful shutdown. The learning
lives in the four verticals — `store` (V1), `streaming` (V2), `index` (V3) and
`multipart` (V4) — and in the SPEC that grades them.

There is **no database and no docker-compose** for the store itself: the
filesystem *is* the store. Everything you would normally get from S3 or MinIO —
content addressing, the durable atomic commit, multipart assembly, prefix
listing, the ETag — is built on plain files here, because the point is that "an
object store" is a layout discipline over a directory rather than a service you
call.

## Run it

```bash
make setup && make sync
make run                                    # the store on :9000
curl -X PUT localhost:9000/my-bucket
curl -X PUT localhost:9000/my-bucket/hello.txt --data-binary @hello.txt
curl      localhost:9000/my-bucket/hello.txt

# the gold standard — point the real AWS CLI at it:
aws --endpoint-url http://localhost:9000 s3 cp ./big.bin s3://my-bucket/big.bin
```

## The three background loops, and why they are tasks rather than threads

The lifecycle sweeper, the blob scrubber and the Haystack compactor all run as
asyncio tasks owned by the lifespan. They spend nearly all their time either
sleeping or inside `asyncio.to_thread`, so they cost one coroutine each and
cannot block the request path. Owning them in the lifespan is what makes
shutdown deterministic: they are cancelled and awaited before the process exits,
so a compaction cannot be interrupted halfway by the interpreter going away.

## Graceful shutdown

Almost all of it is uvicorn's. On SIGTERM it stops accepting connections and
gives in-flight requests `timeout_graceful_shutdown` seconds to finish; the
lifespan's `finally` then cancels the background tasks and closes the Haystack
append handle. The budget is deliberately generous — a request here is an object
transfer, and truncating a 3 GB download to look tidy during a deploy fails
exactly the requests that were most expensive to serve.

## A ceiling worth measuring

Hashing is the CPU-bound work on the PUT path, and `hashlib` releases the GIL
for large buffers, so SHA-256 and MD5 over 64 KB chunks genuinely parallelise
across threads. What will not parallelise is the per-chunk Python overhead: at
small chunk sizes the interpreter, not the disk or the hash, sets the ceiling.
**Do not scale the SPEC's numbers down.** Find where the wall is, work out which
of the usual suspects put it there — GIL contention, GC pressure, allocation
churn in the stream loop, or a blocking call that escaped onto the loop — and
write it up in `docs/06-benchmarks.md`. That finding is the deliverable.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import AuthConfig, ObjectAuthMiddleware
from .config import Settings
from .errors import install_error_handlers
from .index_backend import make_index_backend
from .lifecycle import Lifecycle
from .multipart import Multipart
from .routes import router
from .state import AppState
from .store import Store

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]


def build_state(settings: Settings) -> AppState:
    """Open the store stack. Synchronous, and called before the app serves.

    Opening rebuilds the locator map and the Haystack needle index from disk,
    which is real I/O — doing it here rather than lazily on the first request
    means the port only opens once the store can actually answer, and a corrupt
    data dir fails the process at boot instead of failing one unlucky request
    later.
    """
    store = Store(
        settings.data_dir,
        layout=settings.blob_layout,
        max_volume_size=settings.haystack_max_volume_size,
    )
    index = make_index_backend(settings, store)
    multipart = Multipart(settings.data_dir, store, index)
    lifecycle = Lifecycle(index, store, multipart)
    return AppState(
        settings=settings,
        store=store,
        index=index,
        multipart=multipart,
        lifecycle=lifecycle,
        auth=AuthConfig.from_settings(settings),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can stand up several
    independent stores in one process over different data dirs — which is what
    testing a filesystem-backed store honestly requires.
    """
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        state = await asyncio.to_thread(build_state, config)
        app.state.app_state = state

        tasks = [
            asyncio.create_task(
                state.lifecycle.run_forever(config.lifecycle_scan_interval_secs),
                name="lifecycle-sweeper",
            ),
            asyncio.create_task(
                state.store.run_scrubber(config.scrub_rescan_interval_secs),
                name="blob-scrubber",
            ),
            asyncio.create_task(
                state.store.run_compaction_loop(config.haystack_compaction_interval_secs),
                name="haystack-compaction",
            ),
            asyncio.create_task(state.store.run_checkpoint_loop(), name="haystack-checkpoint"),
        ]

        logger.info(
            "object store opened",
            data_dir=str(config.data_dir),
            blob_layout=config.blob_layout.value,
            max_object_size=config.max_object_size,
            auth="enabled" if state.auth else "disabled (set SECRET_ACCESS_KEY)",
            cdc="enabled" if config.cdc.enabled else "disabled",
        )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            # Gather with `return_exceptions` so one task's cleanup failure
            # cannot stop the others from being awaited — otherwise a shutdown
            # bug in the scrubber would leave the compactor mid-rename.
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await state.index.aclose()
            await asyncio.to_thread(state.store.close)
            logger.info("shutdown complete")

    app = FastAPI(
        title="object-store",
        summary="An S3-compatible object store built on plain files (project 06).",
        lifespan=lifespan,
    )

    # Innermost first: auth must run before a handler streams a body, and the
    # request id must be bound before auth so a rejected request still logs
    # under an id you can find.
    app.add_middleware(ObjectAuthMiddleware, config=AuthConfig.from_settings(config))
    app.add_middleware(common_telemetry.RequestIdMiddleware)

    # The web console fetches cross-origin, and a browser hides the range
    # headers from the page unless they are explicitly exposed — without which
    # resumable download and seeking silently break while everything else looks
    # fine.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "PUT", "POST", "DELETE", "HEAD", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["ETag", "Content-Range", "Content-Length", "Accept-Ranges"],
    )

    install_error_handlers(app)
    # `/metrics` **before** the router: `GET /{bucket}` is a catch-all over one
    # path segment, Starlette matches in registration order, and registering it
    # first turns every scrape into a listing of a bucket named "metrics".
    app.router.routes.extend(common_telemetry.metrics_routes())
    app.include_router(router)
    return app


def main() -> None:
    settings = Settings()
    common_telemetry.init(settings.log_level)
    logger.info(
        "starting",
        addr=f"0.0.0.0:{settings.port}",
        hint="S3 path-style; PUT /{bucket} then PUT /{bucket}/{key}",
    )

    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        # "auto" picks uvloop, which uvicorn[standard] installs. Worth knowing
        # this is *not* the loop pytest runs on, which is why the SPEC's
        # Definition of done asks you to boot the container: some bugs exist
        # under only one of the two.
        loop="auto",
        # RequestIdMiddleware already emits one structured line per request;
        # uvicorn's access log would double the I/O on the hot path.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=int(settings.shutdown_grace_secs),
    )


if __name__ == "__main__":
    main()
