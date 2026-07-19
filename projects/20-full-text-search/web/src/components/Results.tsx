import { useMemo } from 'react'
import { FileText, Trash2 } from 'lucide-react'

import type { SearchResponse } from '@/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { queryTerms, snippet } from '@/highlight'
import { cn } from '@/lib/utils'

interface Props {
  query: string
  result: SearchResponse | null
  error: string | null
  searched: boolean
  onDelete: (id: string) => void
}

/** The ranked hit list. Each hit shows a relevance bar (score ÷ top score), the
 *  BM25 score, its shard + internal doc id, and a highlighted snippet. */
export function Results({ query, result, error, searched, onDelete }: Props) {
  const terms = useMemo(() => queryTerms(query), [query])
  const maxScore = useMemo(
    () => (result && result.hits.length ? Math.max(...result.hits.map((h) => h.score)) : 0),
    [result],
  )

  if (error) {
    return (
      <div className="border-destructive/40 bg-destructive/10 text-destructive rounded-lg border px-4 py-3 text-sm">
        {error}
      </div>
    )
  }

  if (!searched) {
    return (
      <div className="text-muted-foreground flex flex-col items-center gap-2 py-16 text-center text-sm">
        <FileText className="size-8 opacity-40" />
        <p>Run a search to see ranked results.</p>
        <p className="text-xs">Empty index? Seed the sample corpus from the panel on the right.</p>
      </div>
    )
  }

  if (!result || result.hits.length === 0) {
    return (
      <div className="text-muted-foreground py-16 text-center text-sm">
        No documents matched <span className="text-foreground font-mono">{query}</span>.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="text-muted-foreground flex items-center gap-3 text-xs">
        <span>
          <span className="text-foreground font-medium">{result.total}</span> hit
          {result.total === 1 ? '' : 's'}
        </span>
        <span>·</span>
        <span>
          took <span className="text-foreground font-mono">{result.took_ms}ms</span>
        </span>
      </div>

      {result.hits.map((hit, i) => {
        const rel = maxScore > 0 ? hit.score / maxScore : 0
        return (
          <article
            key={`${hit.shard}-${hit.doc_id}-${i}`}
            className="group bg-card hover:border-ring/40 relative overflow-hidden rounded-lg border p-4 transition-colors"
          >
            {/* Relevance bar: a thin fill along the top proportional to top score. */}
            <div className="bg-muted absolute inset-x-0 top-0 h-0.5">
              <div
                className="bg-primary h-full transition-all"
                style={{ width: `${Math.max(4, rel * 100)}%` }}
              />
            </div>

            <header className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-muted-foreground font-mono text-xs">#{i + 1}</span>
              <Badge variant="secondary" className="font-mono">
                score {hit.score.toFixed(4)}
              </Badge>
              {hit.id && (
                <Badge variant="outline" className="font-mono">
                  {hit.id}
                </Badge>
              )}
              <span className="text-muted-foreground font-mono text-xs">
                shard {hit.shard} · doc {hit.doc_id}
              </span>
              {hit.id && (
                <Button
                  variant="ghost"
                  size="icon"
                  className="text-muted-foreground hover:text-destructive ml-auto size-7 opacity-0 transition-opacity group-hover:opacity-100"
                  title={`Delete "${hit.id}" (tombstone)`}
                  onClick={() => onDelete(hit.id!)}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              )}
            </header>

            <p className={cn('text-sm leading-relaxed', !hit.text && 'text-muted-foreground italic')}>
              {hit.text ? snippet(hit.text, terms) : 'document text not stored by this segment'}
            </p>
          </article>
        )
      })}
    </div>
  )
}
