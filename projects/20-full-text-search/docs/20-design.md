# 20 — Full-Text Search: Design Decisions

> Decision log for the choices the SPEC grades on. Each section is a mini
> decision record: **Context** (the forces) → **Options** → **Decision** (what
> you chose) → **Why** (the tradeoff you accepted). Fill the blanks as you build;
> raw benchmark numbers live in [`20-benchmarks.md`](./20-benchmarks.md), not here.
>
> The Definition of done asks this file for five decisions — the analysis
> contract (V1), the on-disk segment format and mmap read path (V2), the BM25
> `k1`/`b` choice (V3), the merge policy and delete model (V4), and the sharding
> function plus the cross-shard scoring tradeoff (V5) — plus the query-cache
> policy.

---

## V1 — The analysis contract

**Status:** decided and implemented. Every claim below is pinned by a test in
[`tests/test_analyzer.py`](../tests/test_analyzer.py), so changing any of them
means changing a test on purpose rather than by accident.

**Context.** The analyzer turns text into the terms that go into the index and
come back out of a query. Whatever it does **is** the definition of "matches" —
there is no more natural notion of matching underneath to fall back on. One
`Analyzer` instance is shared by the shard (index time) and the coordinator
(query time), so index/query symmetry is structural; what remains is making the
pipeline itself correct.

### The pipeline

| # | Stage | Operation |
|---|-------|-----------|
| 1 | normalize the string | `unicodedata.normalize("NFC", text)` |
| 2 | tokenize | `re.findall(r"\w+")`, pattern compiled once at module level |
| 3 | case-fold | `str.casefold()` when `lowercase` is set |
| 4 | re-normalize the token | NFC again, guarded by `is_normalized` |
| 5 | filter | drop `len < min_token_len`, then drop stop-words |

Order is preserved and the result is a `list`, never a `set`: positions are
implied by index, which is what a phrase query would need.

### Decision 1 — Unicode normal form: **NFC, applied twice**

**Options.** Don't normalize · NFC · NFD · NFKC.

**Decision.** NFC over the whole string *before* tokenizing, then NFC again on
each token *after* folding.

**Why (a) — before tokenizing.** The usual argument is that the two spellings of
`café` compare unequal and hash into different postings lists. True, but the
sharper reason is that the tokenizer does not merely separate them, it *corrupts*
one: a combining accent is not a `\w` character, so the token ends early and the
accent is silently discarded.

| input | code points | tokens |
|-------|-------------|--------|
| NFC `café` | `63 61 66 e9` | `["café"]` |
| NFD `café` | `63 61 66 65 301` | `["cafe"]` ← accent dropped |

**Why (b) — again after folding.** `casefold` is not closed under normalization,
so folding an already-normalized string can *un*-normalize it:

```
casefold("ẛ̣")            -> U+1E61 U+0323     # not NFC
normalize("NFC", …)      -> U+1E69            # a different term
```

Without step 4 the analyzer emits a term that a second pass would normalize into
something else — idempotence would fail on real input, and only on input rare
enough to look like magic when it did. Step 4 makes the property structural.
`is_normalized` is a scan without a copy, so the overwhelmingly common case
(already in form) costs almost nothing.

**Why NFC and not NFKC.** Compatibility folding buys recall by conflating
characters the user may have typed deliberately:

| input | `casefold` alone | NFKC |
|-------|------------------|------|
| `Ｒｕｓｔ` (fullwidth) | `ｒｕｓｔ` | `rust` |
| `①` | `①` | `1` |
| `x²` | `x²` | `x2` |

Note what NFKC is *not* needed for: `casefold` already folds the `ﬁ` ligature to
`fi` on its own, because full case folding decomposes it. The compatibility
question is only ever about the rest of that table. We take the conservative
side — a search for `rust` does not find the fullwidth `Ｒｕｓｔ`. Revisit if a
corpus arrives with CJK-adjacent fullwidth ASCII, where NFKC is the norm.

### Decision 2 — Case folding: **`str.casefold()`, not `str.lower()`**

**Decision.** `casefold`, controlled by `AnalyzerConfig.lowercase`.

**Why.** `lower()` is a *display* transform; `casefold` is the operation Unicode
defines for caseless *matching*. `"Straße".lower()` is `"straße"`, which never
meets `"STRASSE".lower()` → `"strasse"`. Under `casefold` both become `strasse`
and match. Since this is a matching rule, not a rendering one, `casefold` is the
correct primitive.

### Decision 3 — Tokenizer: **`\w+`**

**Options.** `str.split()` · `re.findall(r"\w+")` · the `regex` module's word
boundaries · a real segmenter (ICU).

**Decision.** `re.findall(r"\w+")`, compiled at module level.

**Why.** `str.split()` splits on whitespace only, making `rust,` and `rust`
different terms — wrong immediately. `\w+` fixes punctuation and is one C-level
call per document. It is the blunt instrument, and these are the edges we are
accepting:

| input | terms | consequence |
|-------|-------|-------------|
| `can't` | `can`, `t` | contractions split; `t` is a junk term |
| `C++` | `c` | punctuation-carrying names erode |
| `foo_bar` | `foo_bar` | `_` is a word character, so identifiers survive whole |
| `2026-08-29` | `2026`, `08`, `29` | dates fragment; no date type |
| `東京都` | `東京都` | no spaces in CJK — one token, unusable without a segmenter |

The compile-at-module-level detail is not micro-optimization: this runs once per
document indexed and once per query served, so it sits on both hot paths.

**Upgrade path.** A segmenter, if a CJK corpus ever arrives. That is a change to
what matches, so it is a new contract, not a patch.

### Decision 4 — Stop-words: **a 33-word English list, on by default**

**Decision.** `DEFAULT_STOPWORDS`, applied when `remove_stopwords` is set.

**Why.** The highest-frequency words carry almost no signal and dominate postings
list size. Dropping them trades recall for precision and space: a search for
`the` returns nothing, and a phrase query would find a hole where a stop-word
stood. `frozenset` because membership is tested once per token and the set is
never mutated.

**A coupling worth knowing.** The stop-list is stored folded, so it is only
consulted meaningfully *after* step 3. With `lowercase=False`, `The` no longer
matches `the` and survives as a term — turning off case folding quietly turns off
most of stop-word removal too. The two stages are not independent, and the test
that pins this is deliberate.

### Decision 5 — Minimum token length: **1**

**Decision.** `min_token_len = 1`.

**Why.** `\w+` cannot produce an empty token, so the default drops nothing — the
stage exists as a documented knob rather than an active filter. Raising it is the
cheap way to shed single-character noise (the `t` from `can't`, stray digits) at
the cost of losing real one-character terms, which matters more in CJK than in
English.

### Decision 6 — Stemming: **none**

**Decision.** No stemmer. `running`, `ran` and `runs` are three terms.

**Why.** A stemmer buys recall and costs precision, and Porter in particular is
lossy in ways that are hard to explain to a user (`university` and `universe`
both stem to `univers`). It is a SPEC stretch, not a requirement, and adding one
later is a pure analysis change — reindex and the contract moves with it.

### The idempotence argument

`analyze(" ".join(analyze(t))) == analyze(t)` holds because of two obligations,
not because the tests happen to pass:

1. **Every emitted token re-tokenizes to itself.** Tokens are `\w+` and the join
   separator is a space, which `\w` never matches. This breaks the moment a stage
   emits a token containing a separator — a shingle or n-gram filter would do
   exactly that, so it cannot be added without revisiting this.
2. **Every token-level transform is idempotent on its own output.** Filters
   trivially are. `casefold` is. Normalization is the one that is not, which is
   what step 4 exists to repair.

**Verification.** `test_analyze_is_idempotent` under hypothesis over `st.text()`,
plus `test_folded_tokens_are_returned_in_normal_form` as a named regression for
obligation 2.

### What the contract costs, in one table

| Stage | Buys | Costs |
|-------|------|-------|
| NFC normalization | equal spellings match; no truncated tokens | one pass over the text |
| NFC, not NFKC | precision: fullwidth/circled stay distinct | recall on compatibility variants |
| casefold | `Rust` finds `rust`; `Straße` finds `STRASSE` | case can never be queried |
| `\w+` | punctuation-insensitive matching | `C++`, contractions, dates, CJK |
| stop-words | smaller postings, better precision | `the` is unsearchable |
| no stemmer | precision; explainable results | `running` does not find `runs` |

---

## V2 — On-disk segment format & the mmap read path

**Context.** Segments are immutable files; a query must be answerable without
reading the whole segment into memory, and corruption must be detected on read
rather than served as results.

**Layout.** _(fill in — header, term dictionary, postings, checksum; give byte
offsets and endianness)_

**Decision.** _(what you chose)_

**Why.** _(the tradeoff — seek count vs file size vs build cost)_

**Corruption detection.** _(what is checksummed, when it is verified, what
happens on failure)_

---

## V3 — BM25 `k1` / `b`

**Context.** _(what the parameters control)_

**Decision.** `k1 = ___`, `b = ___`

**Why.** _(what these values assume about document length distribution)_

---

## V4 — Merge policy & delete model

**Context.** _(segment count vs merge cost; tombstones vs rewriting)_

**Trigger.** _(the documented merge condition)_

**Delete model.** _(tombstone representation and when space is reclaimed)_

**Why.** _(the tradeoff)_

---

## V5 — Sharding function & cross-shard scoring

**Routing function.** _(and why it must be stable across processes)_

**Cross-shard IDF caveat.** _(what is wrong about per-shard IDF, how wrong it is
in practice, and why it is accepted)_

---

## Query cache policy

**Keyed on.** _(what goes into the key)_

**Invalidation.** _(what a refresh, merge, or delete must do to it)_

**Bound.** _(size/eviction, and what happens at capacity)_

---

## Open questions / deferred

- **The normal form is not configurable.** `lowercase`, `remove_stopwords`,
  `min_token_len` and `stopwords` live in `AnalyzerConfig`; NFC is a module
  constant. So NFC-vs-NFKC is the one analysis stage whose effect cannot be
  observed by toggling a setting — it is pinned by test instead. Promote it to
  the config if a corpus ever needs NFKC.
- **No stemmer** (V1 stretch) — see Decision 6.
- **No segmenter**, so CJK is effectively unsearchable — see Decision 3.
- **Phrase queries** would need the positions the term stream already implies;
  nothing stores them yet.
