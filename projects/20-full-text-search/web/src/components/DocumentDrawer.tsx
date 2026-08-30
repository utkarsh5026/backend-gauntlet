import { useEffect, useMemo } from 'react'
import { Trash2, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { tokenStream } from '@/highlight'
import type { StoredDoc } from '@/lib/docstore'

interface Props {
  doc: StoredDoc | null
  onClose: () => void
  onDelete: (id: string) => void
}

/** The right-hand reading panel.
 *
 *  Two things worth seeing about a document: the text you actually wrote, and
 *  what the engine turned it into. The second is the whole of V1 made visible —
 *  the index does not store your sentence, it stores this bag of lowercased,
 *  stop-word-stripped terms, and a query only matches if it analyzes down to the
 *  same terms.
 */
export function DocumentDrawer({ doc, onClose, onDelete }: Props) {
  // Escape closes, which is what every drawer on the web does.
  useEffect(() => {
    if (!doc) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [doc, onClose])

  const tokens = useMemo(() => (doc ? tokenStream(doc.text) : []), [doc])
  const kept = tokens.filter((t) => t.kept)
  const dropped = useMemo(
    () => [...new Set(tokens.filter((t) => !t.kept).map((t) => t.token))],
    [tokens],
  )
  const unique = useMemo(() => new Set(kept.map((t) => t.token)).size, [kept])

  if (!doc) return null

  return (
    <>
      {/* Backdrop: only on small screens, where the panel covers the page. */}
      <div
        className="bg-background/70 fixed inset-0 z-40 backdrop-blur-sm lg:hidden"
        onClick={onClose}
      />

      <aside className="bg-card fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l shadow-2xl">
        <header className="flex items-start justify-between gap-3 border-b px-5 py-4">
          <div className="min-w-0">
            <p className="text-muted-foreground text-xs">Document</p>
            <h2 className="truncate font-mono text-sm font-semibold">{doc.id}</h2>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} className="size-7 shrink-0">
            <X className="size-4" />
          </Button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="text-muted-foreground mb-5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px]">
            <span>{doc.text.length} characters</span>
            <span>{kept.length} terms</span>
            <span>{unique} unique</span>
            {doc.shard !== null && <span>shard {doc.shard}</span>}
          </div>

          <section className="mb-6">
            <h3 className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
              The text you added
            </h3>
            <p className="bg-background rounded-lg border p-3 text-sm leading-relaxed">{doc.text}</p>
          </section>

          <section>
            <h3 className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
              What the engine indexes
            </h3>
            <p className="text-muted-foreground mb-3 text-xs leading-relaxed">
              Your sentence is not stored as a sentence. It is split on word boundaries,
              lowercased, and common words are thrown away — and it is only these terms that a
              query can ever match.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {kept.map((t, i) => (
                <span
                  key={`${t.token}-${i}`}
                  className="bg-muted rounded px-1.5 py-0.5 font-mono text-[11px]"
                >
                  {t.token}
                </span>
              ))}
            </div>

            {dropped.length > 0 && (
              <div className="mt-4">
                <p className="text-muted-foreground mb-2 text-xs">
                  Dropped as stop words — too common to carry any signal:
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {dropped.map((t) => (
                    <span
                      key={t}
                      className="text-muted-foreground rounded border border-dashed px-1.5 py-0.5 font-mono text-[11px] line-through"
                    >
                      {t}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <p className="text-muted-foreground mt-4 text-[11px] leading-relaxed">
              Computed in the browser as an approximation of{' '}
              <span className="font-mono">src/full_text_search/analyzer.py</span>. If you add a
              stemmer there, teach <span className="font-mono">src/highlight.tsx</span> the same one
              or these will drift apart.
            </p>
          </section>
        </div>

        <footer className="border-t px-5 py-3">
          <Button
            variant="outline"
            size="sm"
            onClick={() => onDelete(doc.id)}
            className="hover:text-destructive w-full"
          >
            <Trash2 className="size-3.5" /> Delete from the index
          </Button>
        </footer>
      </aside>
    </>
  )
}
