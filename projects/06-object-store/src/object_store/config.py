"""Typed settings for the object store.

There is no database and no docker-compose dependency here — **the filesystem is
the store** — so every field below is either a port, a path, or a policy knob.
Each maps to a variable in `.env.example`, and the type annotation is the
parser: `port: int = 9000` gets the env lookup, the string→int coercion, the
default, and a startup error naming the offending variable in one line.

Rust read `std::env::var` from inside `Store::open`, `Haystack::open` and
`CdcConfig::from_env`, which meant three modules could disagree about what the
process was configured to do and tests had to manipulate the environment to
steer them. Here every knob is resolved **once** into this object and passed
down. A handler never reads the environment; it reads state.

## Why `data_dir` is resolved at startup, and why that is a security decision

`DATA_DIR` arrives as a string and becomes one resolved absolute `Path` here.
That single `.resolve()` is what makes "a blob can never escape DATA_DIR"
checkable at all: containment is a claim about two absolute paths, and a
relative one depends on the process's working directory, which can change under
you. Resolve once and the question has an answer. (The keyspace has a second,
independent guard — `naming.encode_key` — because a key is client input in a way
that a config value is not.)
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from common_config import BaseConfig
from pydantic import Field, field_validator

__all__ = [
    "BlobLayoutKind",
    "CdcSettings",
    "Settings",
]

DEFAULT_PORT = 9000
"""The MinIO / s3-local convention, so `aws --endpoint-url` demos read normally."""

DEFAULT_MAX_OBJECT_SIZE = 5 * 1024 * 1024 * 1024
"""S3's single-PUT ceiling (5 GiB). Enforced in V2's stream loop, not by the
framework: Starlette will happily hand you a body of any size, so the running
byte count in `streaming` is the only real limit."""

DEFAULT_MAX_VOLUME_SIZE = 1024 * 1024
"""Haystack volume soft cap (1 MiB). Needles that do not fit fall back to
FileCas under the haystack/hybrid policies."""

DEFAULT_MIN_CHUNK = 8 * 1024
DEFAULT_AVG_CHUNK = 64 * 1024
DEFAULT_MAX_CHUNK = 256 * 1024
DEFAULT_MIN_OBJECT_FOR_CDC = 256 * 1024


class BlobLayoutKind(StrEnum):
    """Write-placement policy for new blobs.

    Only the **write** side: both physical backends are always opened, and reads
    follow the locator map, so flipping this never strands bytes already on
    disk.
    """

    FILE_CAS = "file_cas"
    """Every commit lands under `objects/` — the classic one-file-per-digest CAS."""

    HAYSTACK = "haystack"
    """Pack into a volume when the framed needle fits the soft cap; oversized
    blobs fall back to FileCas."""

    HYBRID = "hybrid"
    """Same packing rule as `HAYSTACK`, under a name that says out loud that
    both trees are in use."""

    @property
    def packs_small(self) -> bool:
        """Whether this policy may pack small objects into Haystack volumes."""
        return self is not BlobLayoutKind.FILE_CAS

    @classmethod
    def _missing_(cls, value: object) -> BlobLayoutKind | None:
        """Accept the ops-friendly aliases people actually type."""
        aliases = {
            "files": cls.FILE_CAS,
            "cas": cls.FILE_CAS,
            "": cls.FILE_CAS,
            "needles": cls.HAYSTACK,
            "volumes": cls.HAYSTACK,
        }
        if isinstance(value, str):
            return aliases.get(value.strip().lower())
        return None


class CdcSettings(BaseConfig):
    """Content-defined chunking — **not** Change Data Capture.

    Off by default: whole-object CAS is the graded path, and chunk-level dedup
    is a From-the-field lab layered on top. Nested under `Settings.cdc` with the
    `CDC_` prefix, so `CDC_ENABLED=1` and `CDC_AVG_CHUNK=131072` work the way an
    operator expects.
    """

    model_config = BaseConfig.model_config | {"env_prefix": "CDC_"}

    enabled: bool = False
    """When false, PUT always takes the whole-object path."""

    min_chunk: int = Field(default=DEFAULT_MIN_CHUNK, gt=0)
    """Never emit a chunk shorter than this, except the final one at EOF."""

    avg_chunk: int = Field(default=DEFAULT_AVG_CHUNK, gt=0)
    """Target average chunk size — this is what sets the cut-point mask width."""

    max_chunk: int = Field(default=DEFAULT_MAX_CHUNK, gt=0)
    """Force a cut once a chunk grows this large without a natural boundary, so
    a pathological input cannot buffer unboundedly."""

    min_object: int = Field(default=DEFAULT_MIN_OBJECT_FOR_CDC, ge=0)
    """Skip CDC below this object size — one whole-object blob is cheaper than a
    manifest plus chunks."""

    def should_chunk(self, logical_size: int) -> bool:
        """Whether an object of `logical_size` bytes should go through CDC."""
        return self.enabled and logical_size >= self.min_object


class Settings(BaseConfig):
    """Every knob this process has, resolved once at startup."""

    port: int = Field(default=DEFAULT_PORT, gt=0, lt=65536)
    """Port the S3 HTTP API binds."""

    index_port: int = Field(default=9106, gt=0, lt=65536)
    """Port the separate `object-store-index` process binds (From the field)."""

    data_dir: Path = Path("./data")
    """Root of everything. Creates `objects/` (FileCas blobs), `volumes/`
    (Haystack needles), `tmp/` (in-flight writes), `cold/` (tiered blobs),
    `quarantine/` (scrub failures), `index/` (key→digest rows) and `uploads/`
    (multipart staging) under itself."""

    max_object_size: int = Field(default=DEFAULT_MAX_OBJECT_SIZE, gt=0)
    """Hard cap on one object or one multipart part, enforced mid-stream."""

    blob_layout: BlobLayoutKind = BlobLayoutKind.FILE_CAS
    """Which physical layout new commits go to."""

    haystack_max_volume_size: int = Field(default=DEFAULT_MAX_VOLUME_SIZE, gt=0)
    """Soft cap on one `volumes/*.dat` file, in bytes."""

    index_url: str = ""
    """Empty → an in-process index. Set → talk to `object-store-index` over
    HTTP, with blobs still living under this process's `data_dir`."""

    access_key_id: str = "local"
    secret_access_key: str = ""
    """Unset/empty → object routes are open (dev and tests). Set → they are
    gated by presigned URL or bearer credentials. Never logged."""

    lifecycle_scan_interval_secs: float = Field(default=60.0, gt=0)
    scrub_rescan_interval_secs: float = Field(default=300.0, gt=0)
    haystack_compaction_interval_secs: float = Field(default=300.0, gt=0)
    shutdown_grace_secs: float = Field(default=30.0, gt=0)
    """How long in-flight streams get to finish after SIGTERM. Generous because
    a request here is an object transfer: cutting a 3 GB download at 5 seconds
    to look tidy fails exactly the requests that were most expensive to serve."""

    log_level: str = "info"

    cdc: CdcSettings = Field(default_factory=CdcSettings)

    @field_validator("data_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        """Pin the data root to one absolute path at startup.

        `strict=False` semantics on purpose — the directory legitimately may not
        exist yet, the store creates it. What matters is that it stops being
        relative here, so every later containment check has something fixed to
        compare against.
        """
        return value.expanduser().resolve()

    @property
    def auth_enabled(self) -> bool:
        """Whether object routes are gated. An empty secret means open."""
        return bool(self.secret_access_key)
