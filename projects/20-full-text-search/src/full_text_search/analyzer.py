"""V1 — The analyzer: text → terms.

This is the part you would normally get from Lucene's analysis chain. An analyzer
turns a blob of text into the sequence of terms that go into (index time) or
query (query time) the inverted index. What it does **is** the definition of
"matches": if it case-folds and strips punctuation, then `"Rust!"` and `"rust"`
become the same term and therefore match; if it doesn't, they don't.

The one rule that makes search work — and the trap if you get it wrong:
**the same analysis runs at index time and query time.** Index `"Running"` as
`running` but analyze the query `"running"` as `Running`, and the lookup misses
every time, silently. One `Analyzer` instance is shared by both paths (the shard
analyzes documents, the coordinator analyzes queries), so whatever you build here
is symmetric by construction. Your job is to make the pipeline itself correct.

A classic pipeline: split into tokens on word boundaries → normalize case → drop
tokens shorter than a floor and any stop-word → optionally stem
(`running` → `run`). Each stage is a deliberate recall-vs-precision trade you
will document in `docs/20-design.md`.

**Three Python-specific things that decide whether this is correct:**

1.  `str.lower()` is not case folding. `"STRASSE".lower()` is `"strasse"` but
    `"Straße".lower()` is `"straße"` — the two never match. `str.casefold()` is
    the operation Unicode defines for caseless comparison, and it is what you
    want when "case-insensitive" is a *matching* rule rather than a display one.

2.  The same visible text can be several different `str` values. `"café"` may be
    `e` + a combining accent, or a single precomposed `é`; they compare unequal
    and hash differently, so they land in different postings lists.
    `unicodedata.normalize` is the fix, and *which* form you pick (NFC keeps
    characters composed, NFKC also folds `ﬁ`→`fi` and `①`→`1`) is part of the
    contract you document.

3.  `str.split()` splits on whitespace, so `"rust,"` and `"rust"` are different
    terms. A `re` character-class pattern (`re.findall`) is the usual next step;
    `regex` module word boundaries or a real segmenter is what you need before
    CJK, which has no spaces at all. Decide how far you go and write it down.

Idempotence, the property the SPEC grades, falls out of getting those right:
re-analyzing the joined output of the analyzer must yield the same terms. If
normalization runs before tokenization but not after, or if a filter can produce
a token the tokenizer would split again, it won't.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .doc import AnalyzedDoc, Term

__all__ = ["DEFAULT_STOPWORDS", "Analyzer", "AnalyzerConfig"]

DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "if", "in",
        "into", "is", "it", "no", "not", "of", "on", "or", "such", "that", "the",
        "their", "then", "there", "these", "they", "this", "to", "was", "will",
        "with",
    }
)  # fmt: skip
"""A small, conventional English stop-word set.

The highest-frequency words carry little signal and bloat postings lists. A
`frozenset` because membership is tested once per token on the hot path and the
set is never mutated — the *choice* of stop list is part of the analysis contract
you document.
"""


@dataclass(frozen=True, slots=True)
class AnalyzerConfig:
    """How the analyzer behaves.

    Frozen because the whole guarantee of V1 is that indexing and querying see
    identical settings; a config that could be mutated after a document was
    indexed would let the two drift apart at runtime, which is exactly the bug
    this vertical exists to prevent.

    The defaults are roughly Elasticsearch's `standard` analyzer: fold case, drop
    English stop-words, keep everything at least one character, no stemmer.
    """

    lowercase: bool = True
    """Case-fold terms so `Rust` and `rust` collapse to one. See the module
    docstring on `casefold` vs `lower`."""

    remove_stopwords: bool = True
    """Drop the words in `stopwords`."""

    min_token_len: int = 1
    """Discard tokens shorter than this many characters (0 keeps everything)."""

    stopwords: frozenset[str] = field(default=DEFAULT_STOPWORDS)
    """The active stop list. Only consulted when `remove_stopwords` is true."""


class Analyzer:
    """The analyzer, shared by the index path and the query path.

    Deliberately holds no mutable state: `analyze` is a pure function of its
    input and the frozen config, so it is safe to call from anywhere — including
    from a worker thread, which is where the scoring path may end up (V5).
    """

    def __init__(self, config: AnalyzerConfig | None = None) -> None:
        self.config = config if config is not None else AnalyzerConfig()

    def analyze(self, text: str) -> list[Term]:
        """Analyze `text` into an ordered stream of terms. **The core of V1.**

        Called from BOTH indexing and querying — keep it pure, and keep it a
        fixed point on its own output.

        TODO(V1): the analysis pipeline. A reasonable order:
          1. Normalize the *string* — `unicodedata.normalize` to one form, so
             two spellings of the same grapheme cannot become two terms.
          2. Tokenize — split into candidate tokens on word boundaries.
             `str.split()` is the starting point and is wrong for punctuation;
             `re.findall(r"\\w+", text)` is the usual next step. Compile the
             pattern once at module level, not per call — this runs on every
             document and every query. Decide what you do about `"can't"`,
             `"C++"` and CJK, and document it.
          3. Normalize the *tokens* — `str.casefold()` when `config.lowercase`.
          4. Filter — drop tokens shorter than `config.min_token_len` and any in
             `config.stopwords` (a `frozenset`, so membership is O(1)).
          5. (Optional stretch) stem — collapse `running`/`ran`/`runs` → `run`.

        Order matters and is preserved: the returned list is the token *stream*,
        with positions implied by index. Phrase queries (a stretch) need those
        positions, so do not silently return a set.
        """
        raise NotImplementedError("V1: tokenize + normalize + filter `text` into the term stream")

    def analyze_doc(self, text: str) -> AnalyzedDoc:
        """Analyze a document for indexing: run `analyze`, then collapse the
        stream into per-term frequencies and record the document length.

        This is the shape a segment stores (V2) and BM25 scores from (V3).

        Implemented in terms of `analyze` on purpose, so index-time and
        query-time analysis can never drift: finish `analyze` and this works for
        free. `Counter` is the stdlib's tally — it is a `dict` subclass, so the
        result is already the `dict[Term, int]` `AnalyzedDoc` wants, and counting
        in C beats a Python loop over a million documents. You *may* rewrite this
        for efficiency, but keep the two symmetric.
        """
        terms = self.analyze(text)
        return AnalyzedDoc(length=len(terms), term_freqs=dict(Counter(terms)))
