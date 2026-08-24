"""Shared vocabulary — the plain data every layer speaks.

Plumbing, not a vertical: these are the shapes that flow between the analyzer
(V1), the on-disk segments (V2), the scorer (V3) and the API. Defining them once
keeps the module boundaries honest.

Two Python notes worth internalising, because they are choices and not accidents:

*   `DocId` and `Term` are **transparent aliases** (`type DocId = int`), not
    wrapper classes. Rust would newtype these to stop you mixing a doc id with a
    segment id; Python's equivalent, `typing.NewType`, buys the same check but
    makes you write `DocId(5)` at every construction site — a per-posting cost in
    a structure with millions of them. The alias documents intent and costs
    nothing. If you later find yourself passing a shard id where a doc id
    belonged, that is the moment to reach for `NewType`.

*   `Posting` is a `NamedTuple`, not a dataclass. A postings list is the single
    most numerous object in the engine, and a `NamedTuple` is a plain tuple at
    runtime — no `__dict__`, no per-instance overhead, and it unpacks. Noticing
    that the hot data structure deserves a different shape from the cold ones is
    part of V2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from pydantic import BaseModel, Field

__all__ = [
    "AnalyzedDoc",
    "CollectionStats",
    "DocId",
    "NewDocument",
    "Posting",
    "SearchHit",
    "ShardId",
    "StoredDoc",
    "Term",
]

type ShardId = int
"""Which shard (V5) a document lives in.

A search fans out to every shard and each hit is tagged with the shard it came
from — internal doc ids are only unique *within* a shard.
"""

type DocId = int
"""A document's internal id, assigned monotonically **by one shard** as it indexes.

It is what appears in a postings list. It is *not* globally unique — two shards
each start at 0 — so a hit always carries its `ShardId` alongside.
"""

type Term = str
"""A single analyzed token — the analyzer's output (V1) and the inverted index's
key (V2). The same term is produced at index time and query time, which is the
whole reason a search matches.
"""


class NewDocument(BaseModel):
    """A document as handed to the engine to index.

    A pydantic model rather than a dataclass because this shape arrives as
    untrusted JSON on `POST /documents` and as one line of NDJSON on `_bulk` —
    parsing and validation are the same step.
    """

    id: str | None = None
    """Optional client-chosen id (Elasticsearch's `_id`).

    When present it decides the doc's shard (stable routing) and lets
    `DELETE /documents/{id}` find it later.
    """

    text: str
    """The text to index. For this lite engine a document is one text field;
    multi-field docs are a stretch."""


@dataclass(slots=True)
class AnalyzedDoc:
    """The result of analyzing one document for indexing.

    Exactly what a segment needs to build its postings (V2) and what BM25 needs
    to score (V3, via the term frequency plus the document length).
    """

    length: int
    """Tokens the analyzer emitted, *before* de-duplication — the document length
    BM25's length-normalization term (`b`) uses."""

    term_freqs: dict[Term, int] = field(default_factory=dict[Term, int])
    """Each distinct term and how many times it occurred in this document.

    A `dict`, not a list of pairs: term lookup during a merge (V4) is a hot
    operation, and since Python 3.7 a dict preserves insertion order, so you keep
    first-occurrence ordering for free if you want it.
    """


@dataclass(slots=True)
class StoredDoc:
    """The stored fields kept for a document so a hit can be rendered without a
    second lookup. Keeping the original `text` here is what lets a search return
    a snippet."""

    id: str | None
    text: str


class Posting(NamedTuple):
    """One entry in a postings list: a document that contains a term, and how
    many times. Read out of a segment (V2), consumed by the scorer (V3)."""

    doc_id: DocId
    term_freq: int


@dataclass(slots=True)
class CollectionStats:
    """Collection-wide statistics BM25 needs (V3).

    Summed across a shard's live segments at query time.
    """

    doc_count: int = 0
    """Number of live documents in the collection — the `N` in IDF."""

    total_length: int = 0
    """Sum of every live document's length, in tokens."""

    @property
    def avg_doc_len(self) -> float:
        """Mean document length — the `avgdl` in the BM25 formula.

        Zero for an empty collection; the scorer must treat that as "nothing to
        rank" rather than dividing by it.
        """
        if self.doc_count == 0:
            return 0.0
        return self.total_length / self.doc_count


class SearchHit(BaseModel):
    """A ranked search result.

    A pydantic model because it is a *response* shape: FastAPI serializes it, and
    `exclude_none` on the optional stored fields keeps the JSON identical to what
    the Rust `serde(skip_serializing_if)` produced — the `web/` client parses it
    unchanged.
    """

    model_config = {"frozen": True}

    shard: ShardId
    """The shard this hit came from — needed because `DocId` is only shard-local."""

    doc_id: DocId
    score: float

    id: str | None = Field(default=None)
    """The client's external id, if the document was indexed with one."""

    text: str | None = Field(default=None)
    """The stored text, for rendering a snippet. Optional so a segment can choose
    not to store it."""
