import { Loader2, RotateCcw, Skull } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useDlq } from '@/hooks/useDlq'
import { timeAgo } from '@/lib/format'

export function DlqPanel() {
  const { jobs, loading, error, refresh, requeueOne, busyId } = useDlq()

  return (
    <Card className="gap-4">
      <CardHeader className="flex-row items-center justify-between">
        <div className="space-y-1.5">
          <CardTitle className="flex items-center gap-2">
            <Skull className="text-chart-dead size-4" />
            Dead-letter queue
            {jobs.length > 0 && <Badge variant="secondary">{jobs.length}</Badge>}
          </CardTitle>
          <CardDescription>Poison jobs that exhausted their attempts — inspect and requeue.</CardDescription>
        </div>
        <Button variant="ghost" size="sm" onClick={refresh} disabled={loading}>
          <RotateCcw /> Refresh
        </Button>
      </CardHeader>
      <CardContent>
        {error && <div className="text-chart-dead mb-3 text-xs">{error}</div>}
        {jobs.length === 0 ? (
          <div className="text-muted-foreground flex h-24 items-center justify-center text-sm">
            {loading ? 'loading…' : 'No dead jobs — nothing has exhausted its retries.'}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-muted-foreground border-border border-b text-left text-xs">
                  <th className="py-2 pr-3 font-medium">ID</th>
                  <th className="py-2 pr-3 font-medium">Queue</th>
                  <th className="py-2 pr-3 font-medium">Kind</th>
                  <th className="py-2 pr-3 font-medium">Attempts</th>
                  <th className="py-2 pr-3 font-medium">Last error</th>
                  <th className="py-2 pr-3 font-medium">Age</th>
                  <th className="py-2" />
                </tr>
              </thead>
              <tbody className="divide-border divide-y">
                {jobs.map((j) => (
                  <tr key={j.id} className="hover:bg-muted/40">
                    <td className="py-2 pr-3 tabular-nums font-medium">{j.id}</td>
                    <td className="py-2 pr-3">
                      <span className="font-mono text-xs">{j.queue}</span>
                    </td>
                    <td className="py-2 pr-3">
                      <span className="font-mono text-xs">{j.kind}</span>
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {j.attempts}/{j.max_attempts}
                    </td>
                    <td className="text-muted-foreground max-w-[24rem] truncate py-2 pr-3 font-mono text-xs">
                      {j.last_error ?? '—'}
                    </td>
                    <td className="text-muted-foreground py-2 pr-3 whitespace-nowrap tabular-nums">
                      {timeAgo(j.created_at)}
                    </td>
                    <td className="py-2 text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busyId === j.id}
                        onClick={() => requeueOne(j.id)}
                      >
                        {busyId === j.id ? <Loader2 className="animate-spin" /> : <RotateCcw />}
                        Requeue
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
