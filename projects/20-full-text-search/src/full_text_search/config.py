"""Typed settings for the search engine.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` starts a usable three-shard index under `./data`.
The type annotation *is* the parser: declaring `shard_count: int` gets you the
env lookup, the coercion, the default, and a startup error naming the offending
variable.

`Field(gt=0)` and friends are not decoration. `SHARD_COUNT=0` would divide by
zero in the router on the first document; catching it here means the process
refuses to start with a clear message instead of failing on request one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from common_config import BaseConfig
from pydantic import Field, field_validator
from pydantic_settings import NoDecode

from .bm25 import Bm25Params
from .shard import EngineConfig

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- HTTP ---
    port: int = 9200
    """HTTP API port. 9200 is the Elasticsearch convention."""

    log_level: str = "info"

    # --- storage (V2 / V5) ---
    index_dir: Path = Path("./data")
    """Where the index lives — `shard-<n>/` subdirectories of immutable segment
    files. The filesystem IS the index; there is no other dependency."""

    shard_count: int = Field(default=3, gt=0)
    """Shards the corpus is partitioned across (V5). Fixed at startup: changing
    it re-hashes every document, which is a reindex, not a config flip."""

    refresh_interval_ms: int = Field(default=0, ge=0)
    """Auto-refresh cadence (V2). 0 disables the background refresher, which is
    the default so the bare scaffold serves without a task raising on the
    not-yet-built flush. Elasticsearch defaults this to 1000ms."""

    # --- BM25 ranking (V3) ---
    bm25_k1: float = Field(default=1.2, ge=0.0)
    bm25_b: float = Field(default=0.75, ge=0.0, le=1.0)

    # --- segment merging (V4) ---
    merge_factor: int = Field(default=10, gt=0)

    # --- query cache (caching horizontal) ---
    query_cache_cap: int = Field(default=0, ge=0)
    """Cached `(query → results)` entries. 0 disables the cache — the default
    while it is unbuilt, so search never touches it."""

    # --- security ---
    api_keys: Annotated[list[str], NoDecode] = []
    """Keys accepted on write/admin routes. Comma-separated in the environment to
    allow rotation. Never logged.

    `NoDecode` is load-bearing, not decoration. pydantic-settings classifies any
    `list[...]` field as "complex" and runs `json.loads` on the raw environment
    string *in the source*, before a single validator gets to see it — so
    `API_KEYS=a,b` dies with a `JSONDecodeError` and the process never starts.
    `NoDecode` turns that off and hands the raw string to the validator below.
    The failure hides well: an empty `API_KEYS=` is skipped entirely, so the
    default configuration boots fine and only a real key list breaks it."""

    max_doc_bytes: int = Field(default=1024 * 1024, gt=0)
    max_query_terms: int = Field(default=64, gt=0)

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_keys(cls, raw: object) -> object:
        """Accept `API_KEYS="a,b,c"` from the environment.

        `mode="before"` so this runs on the raw string rather than on an already
        parsed list. It only ever *sees* that raw string because the field is
        annotated `NoDecode` — see the note on the field itself.
        """
        if not isinstance(raw, str):
            return raw
        return [part.strip() for part in raw.split(",") if part.strip()]

    @property
    def engine(self) -> EngineConfig:
        """Project the flat env-shaped settings into the engine's config.

        Two shapes on purpose: the environment is flat strings, and the engine
        wants grouped, validated values. Keeping the translation here means
        `ShardedIndex` never has to know an environment exists, which is what
        lets a test construct one directly.
        """
        return EngineConfig(
            index_dir=self.index_dir,
            shard_count=self.shard_count,
            bm25=Bm25Params(k1=self.bm25_k1, b=self.bm25_b),
            merge_factor=self.merge_factor,
            max_doc_bytes=self.max_doc_bytes,
            max_query_terms=self.max_query_terms,
            query_cache_cap=self.query_cache_cap,
        )
