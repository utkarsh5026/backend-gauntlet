// Client-side snippet highlighting.
//
// The backend returns the stored `text` but no highlight offsets — so we recover
// which words to <mark> by re-running (an approximation of) the server analyzer on
// the query and wrapping any word in the text whose analyzed form matches a query
// term. This deliberately mirrors src/analyzer.rs so "what gets highlighted" tracks
// "what actually matched": lowercase → split on non-word chars → drop the same
// English stop-words → keep tokens ≥ 1 char.
//
// It is an *approximation*, not the source of truth: the real matcher is the Rust
// analyzer over the inverted index. If you add a stemmer server-side (V1 stretch),
// e.g. `running`→`run`, this highlighter won't stem and would miss `running` for a
// query of `run`. Keep the two in sync, or teach this function the same stemmer.

import * as React from 'react'

// Mirror of DEFAULT_STOPWORDS in src/analyzer.rs — keep in lockstep.
const STOPWORDS = new Set([
  'a', 'an', 'and', 'are', 'as', 'at', 'be', 'but', 'by', 'for', 'if', 'in', 'into', 'is', 'it',
  'no', 'not', 'of', 'on', 'or', 'such', 'that', 'the', 'their', 'then', 'there', 'these',
  'they', 'this', 'to', 'was', 'will', 'with',
])

// A "word" for tokenization: runs of letters/digits (Unicode-aware). Everything
// else (spaces, punctuation) is a separator — matching a split on word boundaries.
const WORD = /[\p{L}\p{N}]+/gu

/** Analyze query text into the set of terms the engine would search for. */
export function queryTerms(query: string): Set<string> {
  const terms = new Set<string>()
  for (const m of query.matchAll(WORD)) {
    const term = m[0].toLowerCase()
    if (term.length >= 1 && !STOPWORDS.has(term)) terms.add(term)
  }
  return terms
}

/**
 * Split `text` into React nodes, wrapping every word that matches a query term in
 * a <mark>. Separators (spaces, punctuation) are preserved verbatim so the snippet
 * reads exactly like the stored text.
 */
export function highlight(text: string, terms: Set<string>): React.ReactNode {
  if (terms.size === 0 || !text) return text

  const nodes: React.ReactNode[] = []
  let last = 0
  let key = 0

  for (const m of text.matchAll(WORD)) {
    const word = m[0]
    const start = m.index ?? 0
    if (terms.has(word.toLowerCase())) {
      if (start > last) nodes.push(text.slice(last, start))
      nodes.push(<mark key={key++}>{word}</mark>)
      last = start + word.length
    }
  }
  if (last < text.length) nodes.push(text.slice(last))

  return nodes
}

/**
 * Build a snippet centered on the first matched term (so long documents don't
 * bury the match below the fold), then highlight it. Returns the whole text when
 * it's already short. `radius` is how many characters of context to keep on each
 * side of the first match.
 */
export function snippet(text: string, terms: Set<string>, radius = 160): React.ReactNode {
  if (terms.size === 0 || text.length <= radius * 2) return highlight(text, terms)

  // Find the first matching word to anchor the window on.
  let anchor = -1
  for (const m of text.matchAll(WORD)) {
    if (terms.has(m[0].toLowerCase())) {
      anchor = m.index ?? 0
      break
    }
  }
  if (anchor === -1) return highlight(text.slice(0, radius * 2) + '…', terms)

  let start = Math.max(0, anchor - radius)
  let end = Math.min(text.length, anchor + radius)
  // Snap to word boundaries so we don't cut a word in half.
  if (start > 0) {
    const sp = text.indexOf(' ', start)
    if (sp !== -1 && sp < anchor) start = sp + 1
  }
  if (end < text.length) {
    const sp = text.lastIndexOf(' ', end)
    if (sp !== -1 && sp > anchor) end = sp
  }

  const body = highlight(text.slice(start, end), terms)
  return (
    <>
      {start > 0 && '… '}
      {body}
      {end < text.length && ' …'}
    </>
  )
}
