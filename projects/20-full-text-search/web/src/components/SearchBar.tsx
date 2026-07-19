import { Search } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

interface Props {
  query: string
  size: number
  loading: boolean
  onQueryChange: (q: string) => void
  onSizeChange: (n: number) => void
  onSubmit: () => void
}

/** The hero search box: a big query input, a top-k selector, and a submit button.
 *  Enter in the input submits (native <form>). */
export function SearchBar({ query, size, loading, onQueryChange, onSizeChange, onSubmit }: Props) {
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit()
      }}
      className="flex flex-col gap-3 sm:flex-row sm:items-center"
    >
      <div className="relative flex-1">
        <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2" />
        <Input
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder="Search the corpus — try “inverted index” or “bm25 ranking”…"
          className={cn('h-12 pl-10 text-base md:text-base', 'font-normal')}
          autoFocus
        />
      </div>
      <div className="flex items-center gap-2">
        <label className="text-muted-foreground text-sm whitespace-nowrap">top-k</label>
        <Input
          type="number"
          min={1}
          max={1000}
          value={size}
          onChange={(e) => onSizeChange(Math.max(1, Math.min(1000, Number(e.target.value) || 1)))}
          className="h-12 w-20 text-center"
        />
        <Button type="submit" size="lg" className="h-12 px-6" disabled={loading || !query.trim()}>
          {loading ? 'Searching…' : 'Search'}
        </Button>
      </div>
    </form>
  )
}
