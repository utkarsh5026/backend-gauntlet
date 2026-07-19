// A tiny demo corpus so the search box has something to rank the moment the page
// loads. "Seed sample corpus" bulk-indexes these, then refreshes. Each doc carries
// an external `id` so it's addressable for delete. The theme is search-engine
// internals — pick queries like "inverted index", "bm25 ranking", "rust async",
// or "merge segments" to see highlighting and relevance ordering light up.
import type { NewDocument } from './api'

export const SAMPLE_CORPUS: NewDocument[] = [
  {
    id: 'inverted-index',
    text: 'An inverted index maps each term to the list of documents that contain it, called a postings list. It is the core data structure that makes full-text search fast: instead of scanning every document, you look up the query terms and intersect their postings.',
  },
  {
    id: 'bm25',
    text: 'BM25 is the ranking function most search engines use to score relevance. It rewards documents where a query term appears often (term frequency) but saturates that reward with the k1 parameter, and it normalizes for document length with the b parameter so long documents are not unfairly favored.',
  },
  {
    id: 'tf-idf',
    text: 'TF-IDF weighs a term by how frequently it appears in a document against how rare it is across the whole collection. Rare terms carry more signal than common ones. BM25 is a probabilistic refinement of the same intuition with tunable saturation and length normalization.',
  },
  {
    id: 'analyzer',
    text: 'An analyzer turns raw text into tokens: it splits on word boundaries, lowercases, removes stop words, and may stem words to their root. The same analysis must run at index time and query time, or a query will never match the terms that were actually stored.',
  },
  {
    id: 'segments',
    text: 'A Lucene-style index is built from immutable segments. Each refresh flushes buffered documents into a new segment so they become searchable. Because segments never change after they are written, reads need no locks and the operating system page cache stays hot.',
  },
  {
    id: 'merging',
    text: 'Many small segments slow down search because every query fans out to all of them. A background merge process compacts small segments into larger ones and physically drops tombstoned documents, trading write amplification for faster reads.',
  },
  {
    id: 'sharding',
    text: 'Sharding partitions a corpus across independent inverted indexes. A search scatters the query to every shard, each returns its local top-k hits, and the coordinator gathers and merges them into a global ranking. Sharding is how search scales past one machine.',
  },
  {
    id: 'deletes',
    text: 'Because segments are immutable, you cannot delete a document in place. Instead you write a tombstone marking the document id as deleted, filter tombstoned docs out of results at query time, and reclaim the space later during a segment merge.',
  },
  {
    id: 'query-cache',
    text: 'A query cache stores the results of recent searches so a repeated query skips scoring entirely. It must be invalidated whenever a refresh or merge changes what is searchable, otherwise the cache would serve stale hits that no longer reflect the index.',
  },
  {
    id: 'rust-async',
    text: 'Rust async with tokio lets the search coordinator fan out to every shard concurrently and await all responses. Because shards are behind an Arc, cloning the handle into each task is cheap, and no data is copied when the futures run in parallel.',
  },
  {
    id: 'relevance',
    text: 'Relevance ranking decides which results a user sees first. A good ranking function balances precision and recall: it surfaces documents that truly match the intent of the query near the top, while still recalling the long tail of weaker matches further down.',
  },
  {
    id: 'stop-words',
    text: 'Stop words like the, a, and is appear in almost every document, so they carry little signal and bloat postings lists. Dropping them during analysis shrinks the index and speeds up queries, at the cost of not being able to search for the exact phrase.',
  },
]
