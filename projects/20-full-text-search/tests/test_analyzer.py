"""V1 acceptance — the analysis contract.

Every test here maps to one of the SPEC's "Done when ALL true" criteria for V1.
Read it as the executable half of `docs/20-design.md`: the analyzer's stages are
recall-vs-precision choices, and a choice you cannot observe changing behaviour
is not a choice — it is an accident. So the sharp edges (`can't`, `C++`, CJK) are
asserted here as *contract*, not tolerated as bugs.
"""

from __future__ import annotations

import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from full_text_search.analyzer import Analyzer, AnalyzerConfig
from full_text_search.shard import ShardedIndex

NFC_CAFE = unicodedata.normalize("NFC", "café")


def test_fixed_input_produces_the_expected_term_stream() -> None:
    """The whole contract in one line: fold case, drop punctuation and stop-words."""
    assert Analyzer().analyze("The Quick, Brown Foxes -- and the DOGS!") == [
        "quick",
        "brown",
        "foxes",
        "dogs",
    ]


@pytest.mark.parametrize("text", ["Rust", "rust", "RUST", "rust!", "...rust...", "(rust)"])
def test_case_and_punctuation_do_not_change_the_term(text: str) -> None:
    assert Analyzer().analyze(text) == ["rust"]


def test_indexed_document_and_query_analyze_to_overlapping_terms() -> None:
    """The match test: what a human calls a hit, the index can actually find."""
    analyzer = Analyzer()
    doc = analyzer.analyze_doc("The Running Foxes are Fast!")
    query = analyzer.analyze("RUNNING, foxes!")

    assert set(query) <= set(doc.term_freqs)
    # Stop-words are gone from the length too — BM25 (V3) normalises by it, so a
    # length that counted `the` would distort every score in the corpus.
    assert doc.length == 3


def test_order_is_preserved_and_repeats_are_kept() -> None:
    """A stream, not a set: positions are implied by index, which is what a
    phrase query would need, and repeats are what term frequency counts."""
    analyzer = Analyzer()
    assert analyzer.analyze("rust rust fast rust") == ["rust", "rust", "fast", "rust"]
    assert analyzer.analyze_doc("rust rust fast rust").term_freqs == {"rust": 3, "fast": 1}


def test_precomposed_and_decomposed_spellings_are_one_term() -> None:
    """Without normalising *before* tokenising, the decomposed spelling does not
    merely differ — `\\w+` ends the token at the combining accent and drops it,
    silently indexing `cafe`."""
    analyzer = Analyzer()
    assert analyzer.analyze(unicodedata.normalize("NFD", "Café")) == [NFC_CAFE]
    assert analyzer.analyze(unicodedata.normalize("NFC", "CAFÉ")) == [NFC_CAFE]


def test_case_folding_is_not_lowercasing() -> None:
    """`str.lower()` leaves `Straße` as `straße`, which never meets `STRASSE`.
    `casefold` is the operation Unicode defines for caseless matching."""
    analyzer = Analyzer()
    assert analyzer.analyze("Straße") == analyzer.analyze("STRASSE") == ["strasse"]


def test_nfkc_folding_is_deliberately_not_applied() -> None:
    """NFC, not NFKC: fullwidth and circled forms stay distinct from their ASCII
    counterparts, so `\uff32\uff55\uff53\uff54` does not find `rust`. Pinned so that widening to
    NFKC stays a decision someone makes on purpose rather than a silent default.
    """
    analyzer = Analyzer()
    assert analyzer.analyze("Ｒｕｓｔ") == ["ｒｕｓｔ"]
    assert analyzer.analyze("① rust") == ["①", "rust"]


def test_ligatures_fold_via_casefold_not_normalization() -> None:
    """Worth pinning because it is easy to misattribute: `\ufb01` becomes `fi` even
    under NFC, since full case folding decomposes the ligature itself. Nothing
    about the NFC-over-NFKC choice prevents it."""
    assert Analyzer().analyze("ﬁle") == ["file"]


def test_folded_tokens_are_returned_in_normal_form() -> None:
    """The subtle one. `casefold` is not closed under NFC, so a term produced by
    folding an already-normalised string can itself be un-normalised — and the
    next pass would normalise it into a different term."""
    terms = Analyzer().analyze("ẛ̣")
    assert all(unicodedata.is_normalized("NFC", term) for term in terms)


@given(st.text())
def test_analyze_is_idempotent(text: str) -> None:
    """Analysis is a fixed point on its own output: re-analysing the joined term
    stream yields that same stream. This is what stops the index and the query
    from drifting apart on any input a user can actually type."""
    analyzer = Analyzer()
    terms = analyzer.analyze(text)
    assert analyzer.analyze(" ".join(terms)) == terms


@given(st.text())
def test_analyze_doc_agrees_with_analyze(text: str) -> None:
    """`analyze_doc` must be a pure summary of `analyze` — the two cannot drift."""
    analyzer = Analyzer()
    doc = analyzer.analyze_doc(text)
    assert doc.length == len(analyzer.analyze(text))
    assert sum(doc.term_freqs.values()) == doc.length


def test_engine_shares_one_analyzer_between_indexing_and_querying(
    engine: ShardedIndex,
) -> None:
    """Not two code paths that can drift: the identical object serves both."""
    assert all(shard.analyzer is engine.analyzer for shard in engine.shards)


def test_lowercasing_toggles_matching() -> None:
    analyzer = Analyzer(AnalyzerConfig(lowercase=False))
    assert analyzer.analyze("The Rust IS Fast") == ["The", "Rust", "IS", "Fast"]


def test_stopword_removal_toggles_matching() -> None:
    assert Analyzer(AnalyzerConfig(remove_stopwords=False)).analyze("the rust") == [
        "the",
        "rust",
    ]
    assert Analyzer().analyze("the rust") == ["rust"]


def test_min_token_len_toggles_matching() -> None:
    assert Analyzer().analyze("go rust") == ["go", "rust"]
    assert Analyzer(AnalyzerConfig(min_token_len=3)).analyze("go rust") == ["rust"]


def test_stop_list_is_a_configured_choice_not_a_constant() -> None:
    analyzer = Analyzer(AnalyzerConfig(stopwords=frozenset({"rust"})))
    assert analyzer.analyze("the rust is fast") == ["the", "is", "fast"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("can't", ["can", "t"]),  # the apostrophe splits the contraction
        ("C++", ["c"]),  # punctuation-only names erode to nothing useful
        ("foo_bar", ["foo_bar"]),  # `_` is a word character, so this stays whole
        ("2026-08-29", ["2026", "08", "29"]),  # digits are terms; the dashes split
        ("東京都", ["東京都"]),  # no spaces in CJK: one token, needs a segmenter
    ],
)
def test_tokenizer_edges_are_contract(text: str, expected: list[str]) -> None:
    """Documented in `Analyzer.analyze` and `docs/20-design.md`. These assertions
    exist so that fixing any of them is a deliberate change to what matches."""
    assert Analyzer().analyze(text) == expected
