import { useMemo } from 'react'
import { Trash2 } from 'lucide-react'

import type { SearchResponse } from '@/api'
import { Button } from '@/components/ui/button'
import { queryTerms, snippet } from '@/highlight'
import type { StoredDoc } from '@/lib/docstore'

interface Props {
  query: string
  result: SearchResponse | null
  onDelete: (id: string) => void
  /** Open a hit in the side panel. A hit carries the stored text, so it can be
   *  read in full there — including documents this browser never added. */
  onOpen: (doc: StoredDoc) => void
}

/** The ranked hit list.
 *
 *  Reordered from the old version so the *document* is what you read first. The
 *  score, shard and internal doc id are engine bookkeeping: still there, because
 *  seeing which shard answered is half the point of V5, but demoted to one quiet
 *  line under the text instead of a row of badges above it.
 */
export function Results({ query, result, onDelete, onOpen }: Props) {
  const terms = useMemo(() => queryTerms(query), [query])
  const maxScore = useMemo(
    () => (result && result.hits.length ? Math.max(...result.hits.map((h) => h.score)) : 0),
    [result],
  )

  if (!result) return null

  if (result.hits.length === 0) {
    return (
      <div className="text-muted-foreground mt-4 rounded-lg border border-dashed py-10 text-center text-sm">
        <p>
          Nothing matched <span className="text-foreground font-medium">{query}</span>.
        </p>
        <p className="mt-1 text-xs">
          Every word may be missing from the index — or still waiting in memory. Try step 2.
        </p>
      </div>
    )
  }

  return (
    <div className="mt-5 flex flex-col gap-2.5">
      <p className="text-muted-foreground text-xs">
        <span className="text-foreground font-medium">{result.total}</span>{' '}
        {result.total === 1 ? 'match' : 'matches'}, best first · found in{' '}
        <span className="text-foreground font-mono">{result.took_ms}ms</span>
      </p>

      {result.hits.map((hit, i) => {
        const rel = maxScore > 0 ? hit.score / maxScore : 0
        return (
          <article
            key={`${hit.shard}-${hit.doc_id}-${i}`}
            className="group bg-background hover:border-ring/40 rounded-lg border p-4 transition-colors"
          >
            {hit.text ? (
              <button
                type="button"
                onClick={() =>
                  onOpen({
                    id: hit.id ?? `shard-${hit.shard}-doc-${hit.doc_id}`,
                    text: hit.text!,
                    shard: hit.shard,
                    addedAt: Date.now(),
                  })
                }
                className="w-full cursor-pointer text-left text-sm leading-relaxed"
                title="Open in the side panel"
              >
                {snippet(hit.text, terms)}
              </button>
            ) : (
              <p className="text-muted-foreground text-sm italic">
                this segment did not store the document text
              </p>
            )}

            <footer className="mt-3 flex items-center gap-3">
              {/* Relevance, relative to the best hit — easier to read at a glance
                  than a raw BM25 score, which has no natural upper bound. */}
              <div className="bg-muted h-1 w-20 shrink-0 overflow-hidden rounded-full">
                <div className="bg-primary h-full" style={{ width: `${Math.max(5, rel * 100)}%` }} />
              </div>
              <span className="text-muted-foreground font-mono text-[11px]">
                {Math.round(rel * 100)}% as relevant as the top hit · score{' '}
                {hit.score.toFixed(3)}
              </span>
              {hit.id && (
                <span className="text-muted-foreground ml-auto flex items-center gap-2 font-mono text-[11px]">
                  <span>
                    {hit.id} · shard {hit.shard}
                  </span>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="hover:text-destructive size-6 opacity-0 transition-opacity group-hover:opacity-100"
                    title={`Delete "${hit.id}"`}
                    onClick={() => onDelete(hit.id!)}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </span>
              )}
            </footer>
          </article>
        )
      })}
    </div>
  )
}
