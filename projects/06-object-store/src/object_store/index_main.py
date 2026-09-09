"""The index microservice binary — From the field, ungraded.

Owns the on-disk `(bucket, key) → blob` map under `DATA_DIR/index`. The S3
front-end keeps blobs in its own `Store` and, when `INDEX_URL` is set, calls
this process over HTTP for every metadata operation.

Both processes point at the **same** `DATA_DIR`, but at disjoint subtrees:
blobs under `objects/` and `volumes/` belong to the front-end, and `index/`
belongs to this one. That separation is what makes the split honest — kill this
process mid-PUT and the blob is already durable while the pointer never appears,
which is exactly the distributed form of V3's blob-then-pointer invariant.

It still opens a `Store`, because GC needs to remove blobs it decides are
unreferenced. See `docs/05-how-index-as-a-service-works.md`.
"""

from __future__ import annotations

import common_telemetry
import structlog
import uvicorn

from .config import Settings
from .index import Index
from .index_server import create_index_app
from .store import Store

logger = structlog.get_logger(__name__)

__all__ = ["main"]


def main() -> None:
    settings = Settings()
    common_telemetry.init(settings.log_level)

    store = Store(settings.data_dir, layout=settings.blob_layout)
    index = Index(settings.data_dir, store)
    logger.info(
        "index service opened (metadata only; blobs stay with the front-end)",
        data_dir=str(settings.data_dir),
    )

    app = create_index_app(index)
    app.router.routes.extend(common_telemetry.metrics_routes())

    logger.info(
        "index service listening (internal /v1 JSON API, not S3 path-style)",
        addr=f"0.0.0.0:{settings.index_port}",
    )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.index_port,
        loop="auto",
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=int(settings.shutdown_grace_secs),
    )


if __name__ == "__main__":
    main()
