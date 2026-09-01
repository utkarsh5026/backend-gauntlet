"""Typed settings — every dial the engine and the RESP front-end read.

One field per variable in `.env.example`, and the type annotation *is* the
parser: `resp_port: int = 6379` gets you the env lookup, the string->int
coercion, the default, and a startup error naming the offending variable.

The names deliberately echo the systems these dials came from, because the
tradeoffs are the same and the reading you will do is written in their
vocabulary: `wal_sync` is redis's `appendfsync`, `memtable_max_bytes` is
RocksDB's `write_buffer_size`, `l0_compaction_trigger` is its
`level0_file_num_compaction_trigger`, `block_cache_bytes` its block cache, and
`max_request_bytes` is redis's `proto-max-bulk-len`.

## The one field that is not a number

`requirepass` is a secret. It is a plain `str` here rather than a
`pydantic.SecretStr` for a reason worth knowing rather than copying: `SecretStr`
protects you from *accidentally* printing it (its `repr` is `**********`), and
that protection is real — but the comparison in `server.py` needs the bytes, so
the moment you call `.get_secret_value()` on the auth path the protection is
gone anyway. The rule that actually holds is the one the SPEC grades: the
password never reaches a log line or `/stats`, which is a property of the code
that touches it, not of its type. `Settings.public_stats()` below is where that
rule is enforced once, so no handler has to remember it.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from common_config import BaseConfig
from pydantic import Field

__all__ = ["Settings", "SyncPolicy"]

DEFAULT_RESP_PORT = 6379
"""Redis' conventional port, so `redis-cli` with no arguments finds *your*
server. The reference redis from docker-compose sits on the project-scoped host
port 6322 instead (`redis-cli -p 6322`), so the two never collide."""

DEFAULT_HTTP_PORT = 8080
"""The observability sidecar. Never data."""


class SyncPolicy(StrEnum):
    """When the WAL forces its bytes to stable storage (V2). Redis calls this
    dial `appendfsync`, and the three settings are the same three tradeoffs."""

    ALWAYS = "always"
    """`fsync` before acknowledging every write — safest, and capped by the
    disk's sync rate rather than by anything Python does."""

    EVERYSEC = "everysec"
    """`fsync` at most once a second from a background task; a crash loses at
    most that window. The redis default, and this one."""

    NO = "no"
    """Never `fsync` explicitly — the OS page cache decides. Fastest, and the
    only policy under which an acknowledged write can vanish on a power cut with
    nothing wrong in your code."""


class Settings(BaseConfig):
    # --- listeners ---

    resp_port: int = Field(default=DEFAULT_RESP_PORT, ge=0, lt=65536)
    """The RESP data plane. Real clients connect here.

    `0` is allowed and means "let the OS pick a free port" — which is how the
    tests bind without racing each other for 6379, and how you run two engines
    side by side to compare configurations."""

    http_port: int = Field(default=DEFAULT_HTTP_PORT, gt=0, lt=65536)
    """The HTTP sidecar: `/healthz`, `/stats`, `/metrics`."""

    log_level: str = "info"

    # --- storage ---

    data_dir: Path = Path("./data")
    """Where the WAL and the SSTable files live. This directory *is* the
    database — there is no Postgres, no Redis, nothing else to configure."""

    memtable_max_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    """Freeze and flush the active memtable once it holds this many approximate
    bytes (V3 -> V4). Small here (4 MiB) so a modest test produces several
    SSTables and actually exercises compaction; RocksDB defaults to 64 MiB."""

    wal_sync: SyncPolicy = SyncPolicy.EVERYSEC
    """The durability/throughput dial (V2)."""

    # --- SSTable / bloom / compaction ---

    block_size_bytes: int = Field(default=4096, gt=0)
    """Target size of an SSTable data block (V4) — the granularity of both a
    disk read and a block-cache entry, so it sets the floor on read
    amplification and the resolution of the cache in one number."""

    bloom_bits_per_key: int = Field(default=10, gt=0)
    """Bloom sizing (V5). ~10 bits/key is about a 1% false-positive rate —
    LevelDB's default, and the number V5's measured-FP-rate criterion checks."""

    l0_compaction_trigger: int = Field(default=4, gt=0)
    """Compact once the youngest level holds this many SSTables (V6). This is
    the count the boss fight watches: it must stay bounded under a sustained
    write flood, or you are in a write stall."""

    run_compaction: bool = False
    """Background compaction is off by default so the bare scaffold does not
    spawn a loop that raises on V6 every tick. Flip it on once V4 (flush) and
    V6 (merge) exist."""

    compaction_interval_ms: int = Field(default=1000, gt=0)
    """How often the background compactor asks whether a level is over its
    trigger."""

    # --- block cache (V7) ---

    block_cache_bytes: int = Field(default=8 * 1024 * 1024, ge=0)
    """Byte budget for the hand-built LRU over decoded SSTable blocks. `0`
    disables the cache entirely (every read goes to disk) — the read path must
    work either way, which is one of V7's criteria."""

    # --- security ---

    requirepass: str = ""
    """Password required before any command runs (redis `requirepass`). Empty =
    open server. Never logged — see the module docstring."""

    max_request_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    """Hard cap on one bulk string's *declared* length (redis
    `proto-max-bulk-len`), so a hostile `$1000000000000` header is a protocol
    error rather than an allocation. The cap has to be enforced against the
    number in the header, before you reserve anything for it — checking the
    bytes you already read is checking the wrong thing."""

    # --- derived ---

    @property
    def auth_required(self) -> bool:
        return bool(self.requirepass)

    @property
    def compaction_interval(self) -> float:
        """Seconds. The environment speaks milliseconds because that is the
        readable unit for an interval; asyncio speaks seconds as floats
        everywhere, so the conversion happens once, here."""
        return self.compaction_interval_ms / 1000.0

    @property
    def wal_path(self) -> Path:
        return self.data_dir / "wal.log"

    def public_stats(self) -> dict[str, Any]:
        """The subset of configuration that is safe to publish on `/stats`.

        Enumerated positively — an allowlist, not `model_dump()` minus a
        denylist. The two look equivalent until someone adds a second secret
        field, at which point the denylist silently starts leaking and the
        allowlist silently keeps not leaking. `requirepass` is absent from this
        dict, and the smoke tests assert that it stays absent.
        """
        return {
            "data_dir": str(self.data_dir),
            "memtable_max_bytes": self.memtable_max_bytes,
            "wal_sync": self.wal_sync.value,
            "block_size_bytes": self.block_size_bytes,
            "bloom_bits_per_key": self.bloom_bits_per_key,
            "l0_compaction_trigger": self.l0_compaction_trigger,
            "block_cache_bytes": self.block_cache_bytes,
            "max_request_bytes": self.max_request_bytes,
            "auth_required": self.auth_required,
        }
