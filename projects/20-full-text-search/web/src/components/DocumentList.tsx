import { FileText, Trash2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import type { StoredDoc } from '@/lib/docstore'

interface Props {
  docs: StoredDoc[]
  selectedId: string | null
  onSelect: (doc: StoredDoc) => void
  onClear: () => void
}

/** The list of documents this console has added. Click a row to read it in the
 *  side panel. */
export function DocumentList({ docs, selectedId, onSelect, onClear }: Props) {
  if (docs.length === 0) return null

  return (
    <div className="mt-5">
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
          Documents you added ({docs.length})
        </h3>
        <Button
          variant="ghost"
          size="sm"
          onClick={onClear}
          className="text-muted-foreground hover:text-destructive h-6 px-2 text-xs"
        >
          <Trash2 className="size-3" /> Clear list
        </Button>
      </div>

      <ul className="overflow-hidden rounded-lg border">
        {docs.map((doc) => (
          <li key={doc.id}>
            <button
              onClick={() => onSelect(doc)}
              className={
                'hover:bg-accent/50 flex w-full items-center gap-3 border-b px-3 py-2.5 text-left transition-colors last:border-b-0 ' +
                (selectedId === doc.id ? 'bg-accent' : '')
              }
            >
              <FileText className="text-muted-foreground size-3.5 shrink-0" />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-mono text-xs font-medium">{doc.id}</span>
                <span className="text-muted-foreground block truncate text-xs">{doc.text}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>

      <p className="text-muted-foreground mt-2 text-xs leading-relaxed">
        This list lives in your browser, not the engine — an inverted index can tell you which
        documents contain a word, but it cannot hand you a document by id, so there is no endpoint
        to list them.
      </p>
    </div>
  )
}
