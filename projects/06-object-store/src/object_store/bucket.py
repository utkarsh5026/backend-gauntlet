"""Bucket-level metadata — the first durable state a bucket owns.

A bucket is otherwise just the directory `index/<bucket>/`. This adds one
document at its root, `index/<bucket>/metadata.json`, holding what S3 keeps per
bucket. The placement is deliberate: it is a **sibling** of the `objects/`
directory where key rows live, so it can never collide with a user-PUT key —
every key lands under `objects/`, and there is no key whose encoded filename can
escape into the parent.

## Absent means default

Buckets created before this document existed have no file, so `load` returns a
fresh default rather than raising. That is the backward-compatibility contract,
and it is why adding a field here is safe. A file that *exists but will not
parse* is a different thing entirely and does raise — silently discarding a
lifecycle policy would start deleting objects the owner asked to keep.

Only fields with a live reader live here. S3 has ACLs, CORS, encryption
settings and tags; none of them have a consumer in this store yet, and a
metadata field nothing reads is a schema you have to migrate for no benefit.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .durable import atomic_write_sibling
from .lifecycle import LifecyclePolicy
from .objects import utc_now

__all__ = ["CURRENT_SCHEMA_VERSION", "BucketMetadata"]

CURRENT_SCHEMA_VERSION = 1
"""Bump when a change is not purely additive, and branch on it in `load`."""

FILE_NAME = "metadata.json"


class BucketMetadata(BaseModel):
    """What we persist about a bucket."""

    schema_version: int = CURRENT_SCHEMA_VERSION
    """On-disk format version, so the next change is a migration rather than a
    break."""

    created_at: datetime = Field(default_factory=utc_now)
    """When the bucket was created — S3 surfaces this as `CreationDate` in
    `ListBuckets`, and there is no other durable record of a bucket's birth."""

    lifecycle: LifecyclePolicy = Field(default_factory=LifecyclePolicy)
    """The owner's ageing rules. Empty by default: nothing expires unless asked."""

    @classmethod
    def new(cls) -> BucketMetadata:
        """A brand-new bucket's metadata: born now, no rules yet."""
        return cls()

    @staticmethod
    def path_in(bucket_dir: Path) -> Path:
        return bucket_dir / FILE_NAME

    @classmethod
    def load(cls, bucket_dir: Path) -> BucketMetadata:
        """Load a bucket's metadata, defaulting when the file is absent."""
        path = cls.path_in(bucket_dir)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cls.new()

        metadata = cls.model_validate(json.loads(raw))
        if metadata.schema_version < CURRENT_SCHEMA_VERSION:
            metadata.schema_version = CURRENT_SCHEMA_VERSION
            metadata.store(bucket_dir)
        return metadata

    def store(self, bucket_dir: Path) -> None:
        """Durably persist via a sibling temp next to `metadata.json`.

        A sibling is enough here, unlike index rows: GC does not scan bucket
        metadata for digests, so there is nothing to gain from staging it
        somewhere the collector can see.
        """
        atomic_write_sibling(self.path_in(bucket_dir), self.model_dump_json().encode("utf-8"))
