# Analysis: How Text Becomes Terms — From First Principles

> A ground-up guide to the analyzer — the pipeline that turns raw text into the
> **terms** a search engine actually indexes and queries. No prior search-engine
> knowledge assumed. This prepares you for **V1** in [SPEC.md](../SPEC.md): the
> `todo!()` in [`Analyzer::analyze`](../src/analyzer.rs) is the thing you're about
> to build. Anchored to [analyzer.rs](../src/analyzer.rs) and [doc.rs](../src/doc.rs).

---

## 0. The one sentence to hold onto

**A search engine never matches your text — it matches a *derived* version of your
text, and the derivation *is* the definition of "matches".**

If the derivation lowercases, `Rust` finds `rust`. If it strips punctuation,
`rust!` finds `rust`. If it doesn't, they're strangers. There is no "natural"
notion of matching hiding underneath — the analyzer *is* the notion of matching.
Everything else in this doc is consequences of that sentence.

---

## 1. The problem: why not just compare strings?

The obvious design: store documents as-is, and answer a query by checking whether
the query string appears in each document — SQL's `WHERE text LIKE '%rust%'`.
It falls apart for concrete reasons:

| Naive string matching | What breaks |
| --- | --- |
| `LIKE '%rust%'` scans every row | O(corpus) per query, forever. A million documents means a million string scans *per search*. |
| Case matters | A user typing `rust` misses every document that says `Rust`. Half your corpus is invisible depending on the shift key. |
| Punctuation matters | `rust!`, `rust,`, `(rust)` are all different byte sequences. The document "I love Rust!" doesn't contain the substring `rust ` — matches leak away at every comma. |
| Substrings over-match | `LIKE '%cat%'` happily matches `category`, `scatter`, `certificate`. You wanted the *word*, not the byte pattern. |
| Word forms are strangers | "Running shoes" doesn't match a search for `run`. Humans consider that a match; bytes don't. |
| No ranking | `LIKE` returns a boolean per row. Which of 50,000 matches is *best*? String containment has no opinion. |

Every one of these has the same root cause: **raw text is the wrong space to match
in.** The fix is to transform both documents *and* queries into a normalized space
— a sequence of **terms** — and match there. That transformation is the analyzer.

In this project the term type is deliberately dumb — see
[doc.rs](../src/doc.rs):

```rust
/// A single analyzed token — the output of the analyzer (V1) and the key of the
/// inverted index (V2). The same [`Term`] is produced at index time and query time,
/// which is the whole reason a search matches.
pub struct Term(pub String);
```

The intelligence isn't in the type. It's in the pipeline that produces it.

---

## 2. The pipeline: four stages, each a deliberate dial

An analyzer is a pipeline of stages. The classic shape (and the shape the
scaffold's doc comment in [analyzer.rs](../src/analyzer.rs) suggests):

```
  raw text ──► tokenize ──► normalize ──► filter ──► (stem) ──► term stream
              split into    lowercase /   drop stop-   collapse
              candidate     case-fold     words + too-  word forms
              tokens                      short tokens  (stretch)
```

Trace one input through it. Take the document text:

```
"The Quick brown fox JUMPED over the lazy dog!"
```

| Stage | Output |
| --- | --- |
| tokenize | `The` `Quick` `brown` `fox` `JUMPED` `over` `the` `lazy` `dog` |
| lowercase | `the` `quick` `brown` `fox` `jumped` `over` `the` `lazy` `dog` |
| stop-words¹ | `quick` `brown` `fox` `jumped` `over` `lazy` `dog` |
| (stem, stretch) | `quick` `brown` `fox` `jump` `over` `lazi` `dog` |

¹ `the` is in [`DEFAULT_STOPWORDS`](../src/analyzer.rs); `over` is not — check the
list, don't assume. The exact stop list is *part of your analysis contract*.

Now the query `"jumping foxes?"` goes through the **same** pipeline and comes out
as (with a stemmer) `jump` `fox` — and suddenly a document about a fox that
jumped matches a query about jumping foxes, even though the raw strings share
almost nothing. That's the whole trick. Matching happens **in the derived space**.

### Each stage is a recall-vs-precision trade

Two words you need for the rest of your search career:

- **Recall** — of the documents a human would call relevant, how many did we find?
- **Precision** — of the documents we returned, how many are actually relevant?

Every analyzer stage moves the dial between them:

| Stage | Buys recall | Costs precision | Concrete casualty |
| --- | --- | --- | --- |
| lowercase | `rust` finds `Rust`, `RUST` | Case distinctions vanish | `Polish` (the nationality) and `polish` (the furniture verb) become one term |
| stop-word removal | Smaller index, no useless postings for `the` | Some queries become unaskable | "The Who" (the band), "to be or not to be" — every term is a stop-word; the query analyzes to *nothing* |
| min-token-length | Drops noise like stray single chars | Real short tokens die | `C` (the language), `Go` at min length 3 |
| stemming (stretch) | `run`/`running`/`runs` unify | Over-merging | A bad stemmer collapses `university`/`universe` |

There is **no correct setting** — only a *documented* one. That's why V1's
Done-when list says every stage must be a "deliberate, documented choice" whose
effect is observable when toggled. The scaffold makes each stage a config knob for
exactly this reason — see [`AnalyzerConfig`](../src/analyzer.rs):

```rust
pub struct AnalyzerConfig {
    pub lowercase: bool,
    pub remove_stopwords: bool,
    pub min_token_len: usize,
    pub stopwords: HashSet<String>,
}
```

### Tokenization is a stage, not a `.split(' ')`

The first stage looks trivial and isn't. What is a "word boundary" in:

| Input | The question |
| --- | --- |
| `"can't"` | One token or two (`can` + `t`)? |
| `"C++"`, `"C#"` | Punctuation is *part of the name* — strip it and the languages become unsearchable (they all collapse to `c`) |
| `"state-of-the-art"` | One token, four, or both? |
| `"全文検索"` | CJK has **no spaces at all** — whitespace tokenization emits the whole sentence as one giant term |
| `"3.14"` | Split on `.` and you index `3` and `14` |

`split_whitespace` plus stripping non-alphanumerics is a legitimate *starting*
choice for an English-only engine — but notice it's a *choice* that makes `C++`
unfindable, and your design doc should say so. Unicode word segmentation (UAX #29,
what the `unicode-segmentation` crate implements) is the grown-up answer real
engines use. This is precisely the kind of decision V1 wants you to make
consciously rather than inherit from whatever stdlib method you reached for first.

---

## 3. The trap: asymmetry, the silent recall killer

Here is the failure mode this whole vertical is designed to burn into you.
Suppose indexing lowercases but querying (via a different code path, a "quick
fix", a stale config) does not:

```
  INDEX TIME                              QUERY TIME
  "Running is fun"                        query: "Running"
       │ tokenize+lowercase                     │ tokenize (no lowercase!)
       ▼                                        ▼
  terms: running, fun                     terms: Running
       │                                        │
       ▼                                        ▼
  index now contains:                     look up "Running" in the index…
    "running" → [doc 1]                     ┌────────────────────────┐
    "fun"     → [doc 1]                     │ "Running" ∉ dictionary │
                                            │      → 0 results       │
                                            └────────────────────────┘
```

Note everything about this failure:

- **No error.** The lookup is perfectly well-formed — the term just isn't there.
- **No log line.** Nothing crashed. Nothing is "wrong" from the code's view.
- **The user's conclusion:** "the data is gone" or "search is broken". Almost
  every real-world "search can't find my document" ticket is this, one way
  or another.

The fix is structural, not procedural: **one analyzer, both paths — the same
code, not the same configuration.** Two code paths configured identically *today*
will drift *tomorrow*. The scaffold enforces this by construction: a single
`Arc<Analyzer>` is shared into both the indexing path and the query path (see
[`ShardedIndex`](../src/shard.rs), which holds the analyzer and analyzes each
query once at the coordinator), and
[`Analyzer::analyze_doc`](../src/analyzer.rs) is deliberately implemented *in
terms of* `analyze` — finish `analyze` and both paths are symmetric for free.

The V1 corollary worth internalizing: **changing the analyzer invalidates the
index.** Add a stemmer next month and every already-indexed document is on the far
side of an asymmetry (indexed as `running`, queried as `run`). The only fix is a
full reindex — which is why real engines version their analyzers and treat an
analysis change as a migration, not a hotfix.

### Idempotence: the sanity check that catches drift

V1's Done-when list includes a property that looks odd at first:

> Re-analyzing the joined output of the analyzer yields the same terms
> (it's a fixed point on its own output).

Why demand this? Because analyzer *output* re-enters analyzers all the time
(query suggestions built from indexed terms, "more like this" queries built from
a document's own terms). If `analyze("Running!") = [running]` but
`analyze("running") = [runn]` — some stage misfires on already-clean input —
then terms and queries built from index data silently miss. A pipeline whose
output is a fixed point of itself can't do that. It's also a wonderfully cheap
property test: for any input, `analyze(join(analyze(x))) == analyze(x)`.

---

## 4. Frequencies and length: what indexing actually keeps

The stream of terms isn't stored verbatim. For indexing, the scaffold collapses it
— see [`Analyzer::analyze_doc`](../src/analyzer.rs) and
[`AnalyzedDoc`](../src/doc.rs):

```rust
pub struct AnalyzedDoc {
    /// Number of tokens the analyzer emitted (before de-duplication)
    pub length: u32,
    /// Each distinct term and how many times it occurred in this document.
    pub term_freqs: Vec<(Term, u32)>,
}
```

So `"Rust is fast. Rust is safe."` becomes (with default config — `is` is a
stop-word):

```
length: 4                       ← rust, fast, rust, safe = 4 emitted tokens
term_freqs: [(rust, 2), (fast, 1), (safe, 1)]
```

Hold onto both numbers — they are not bookkeeping. The **term frequency** and the
**document length** are exactly the two inputs BM25 (V3) scores with. Notice that
"document length" here means *length after analysis* — stop-words you dropped
don't count. Your analyzer choices literally reshape the ranking math downstream.
The pipeline you build in V1 is the foundation the next four verticals stand on.

---

## 5. The design space you must walk through (not around)

The `todo!()` in [`analyze`](../src/analyzer.rs) is small — but each line of it is
a decision the SPEC grades you on documenting:

1. **What is a token boundary?** Whitespace + punctuation stripping, or Unicode
   segmentation? What happens to `can't`, `C++`, digits?
2. **Which normalizations, exactly?** Lowercase via `to_lowercase()` handles most
   of it — do you care that full case-folding is a different, slightly wider
   operation (`ß` → `ss`)? For this engine, probably not — *say so*.
3. **Which filters, in which order?** Does min-length run before or after
   stop-words? (Does it matter? Work out one input where it would.)
4. **Stop-words: on or off?** The scaffold defaults on. You now know what that
   does to "The Who". Is that acceptable for your corpus? Document the call.

None of these have a right answer, which is why this doc stops here. When you've
made the calls, `/hint` can nudge a stuck implementation and `/quest` runs the
full guided build with acceptance tests written up front.

---

## 6. Mental-model summary

| Idea | One-line version |
| --- | --- |
| Terms, not text | Search matches in a derived space; the analyzer defines that space — and therefore defines "match". |
| Pipeline of dials | tokenize → normalize → filter → (stem); every stage trades recall against precision. |
| Symmetry is structural | One shared `Analyzer` on both paths. Same *code*, not same *config* — configs drift. |
| Asymmetry is silent | The failure is zero results with no error, at the dictionary lookup. Nothing logs it. |
| Analyzer change = reindex | Old documents live on the far side of any analysis change. |
| Idempotence | `analyze` is a fixed point on its own output — a cheap property test that catches misbehaving stages. |
| Output feeds V2 + V3 | `(term, tf)` pairs become postings; the token count becomes BM25's document length. |

## 7. Where you'll build this

- **Module:** [src/analyzer.rs](../src/analyzer.rs) — the `todo!()` in
  `Analyzer::analyze` (and the test-module TODOs below it).
- **Unlocks these V1 Done-when boxes** ([SPEC.md](../SPEC.md)):
  - [ ] Matching document + query produce an overlapping term set.
  - [ ] Analysis is idempotent (`prop_analyze_is_idempotent`).
  - [ ] The same analyzer provably serves indexing and querying.
  - [ ] Every pipeline stage is deliberate, documented, and observable when toggled.
- **Proof artifacts:** fixed-input → expected-stream unit tests, the idempotence
  property test, the overlap test, and the analysis contract written into
  `docs/20-design.md`.
