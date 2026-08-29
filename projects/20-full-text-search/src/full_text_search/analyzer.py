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

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Final

from .doc import AnalyzedDoc, Term

__all__ = ["DEFAULT_STOPWORDS", "Analyzer", "AnalyzerConfig"]

_NORMAL_FORM: Final = "NFC"
"""The Unicode normal form every term is in, at both ends of the pipeline.

NFC and not NFKC. Compatibility folding would additionally collapse fullwidth
`\uff32\uff55\uff53\uff54` to `Rust`, `\u2460` to `1` and `x\u00b2` to `x2` — recall bought by
conflating characters a user may have typed deliberately. That is a larger claim
than caseless matching, and this analyzer does not make it.

Note what NFKC is *not* needed for: `casefold` already folds the `\ufb01` ligature to
`fi` on its own, because full case folding decomposes it. The compatibility
question is only ever about the rest.
"""

_TOKEN_RE: Final = re.compile(r"\w+")
"""Compiled once at import, not per call — this runs on every document indexed
and every query served."""

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
        self._config = config if config is not None else AnalyzerConfig()

    def analyze(self, text: str) -> list[Term]:
        """Analyze `text` into an ordered stream of terms. **The core of V1.**

        Called from BOTH indexing and querying — pure, and a fixed point on its
        own output. The stages sit in this order for reasons, not by habit:

        1.  **Normalize the whole string to NFC, before tokenizing.** Not merely
            so that two spellings of `café` hash alike: the token pattern would
            *truncate* the decomposed spelling, because a combining accent is
            not a `\\w` character. The accent is not merely separated, it is
            dropped.

                NFC  "café"  ->  ["café"]     # é is a single character
                NFD  "café"  ->  ["cafe"]     # the accent ends the token, and is lost

        2.  **Tokenize** on `\\w+`. This is the contract's blunt edge, and it is
            worth being explicit about what it costs: `can't` becomes `can` +
            `t`, `C++` becomes `c`, `foo_bar` survives whole (`_` is a word
            character), and CJK — which has no spaces — returns as one enormous
            token. Acceptable for the English corpus this is benchmarked on; a
            real segmenter is the upgrade path.

        3.  **Case-fold**, not `lower()` — `casefold` is what Unicode defines
            for caseless *matching*, so `Straße` and `STRASSE` become one term
            rather than two that never meet.

        4.  **Re-assert NFC on the folded token.** The subtle step, and the one
            that makes idempotence structural instead of lucky: `casefold` is
            not closed under normalization. `casefold("\u1e9b\u0323")` returns
            `\u1e61\u0323`, which is *not* NFC — so a second pass over this
            function's own output would normalize it to `\u1e69` and yield a
            different term. Folding after normalizing partially undoes the
            normalizing, so the form has to be re-established here.
            `is_normalized` is a cheap scan that skips the copy for the
            overwhelming majority of tokens, which are already in form.

        5.  **Filter** — length before stop-list, because an `int` comparison is
            cheaper than hashing a string to probe a set.

        Order is preserved and the result is a `list`, never a `set`: positions
        are implied by index, which is what a phrase query (stretch) needs.
        """
        config = self._config
        raw_tokens: list[str] = _TOKEN_RE.findall(unicodedata.normalize(_NORMAL_FORM, text))

        terms: list[Term] = []
        for raw in raw_tokens:
            term = raw.casefold() if config.lowercase else raw
            if not unicodedata.is_normalized(_NORMAL_FORM, term):
                term = unicodedata.normalize(_NORMAL_FORM, term)
            if len(term) < config.min_token_len:
                continue
            if config.remove_stopwords and term in config.stopwords:
                continue
            terms.append(term)
        return terms

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
