import { useEffect, useRef, useState } from 'react'
import { Download, Folder, RefreshCw, Trash2, Upload } from 'lucide-react'

import * as api from '../api'
import type { S3Object } from '../api'
import { fmtBytes, fmtDate, errMsg } from '../util'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Progress } from '@/components/ui/progress'

export default function Objects({ bucket }: { bucket: string }) {
  const [prefix, setPrefix] = useState('')
  const [folderMode, setFolderMode] = useState(true)
  const [objects, setObjects] = useState<S3Object[]>([])
  const [prefixes, setPrefixes] = useState<string[]>([])
  const [token, setToken] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [uploadPct, setUploadPct] = useState<number | null>(null)
  const [lastEtag, setLastEtag] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const list = async (continuation?: string, replace = false) => {
    setLoading(true)
    setErr(null)
    try {
      const res = await api.listObjects(bucket, {
        prefix: prefix || undefined,
        delimiter: folderMode ? '/' : undefined,
        continuationToken: continuation,
        maxKeys: 100,
      })
      setObjects((prev) => (replace ? res.objects : [...prev, ...res.objects]))
      setPrefixes((prev) =>
        replace ? res.commonPrefixes : Array.from(new Set([...prev, ...res.commonPrefixes])),
      )
      setToken(res.nextContinuationToken)
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  // Reload from scratch whenever the bucket / prefix / folder-mode changes.
  useEffect(() => {
    setObjects([])
    setPrefixes([])
    setToken(null)
    list(undefined, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bucket, prefix, folderMode])

  const upload = async () => {
    const f = fileRef.current?.files?.[0]
    if (!f) return
    const key = (folderMode ? prefix : '') + f.name
    setUploadPct(0)
    setErr(null)
    setLastEtag(null)
    try {
      const r = await api.putObject(bucket, key, f, (loaded, total) =>
        setUploadPct(Math.round((loaded / total) * 100)),
      )
      setLastEtag(r.etag)
      if (fileRef.current) fileRef.current.value = ''
      await list(undefined, true)
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setUploadPct(null)
    }
  }

  const remove = async (key: string) => {
    if (!confirm(`Delete "${key}"?`)) return
    setErr(null)
    try {
      await api.deleteObject(bucket, key)
      await list(undefined, true)
    } catch (e) {
      setErr(errMsg(e))
    }
  }

  const crumbs = prefix ? prefix.replace(/\/$/, '').split('/') : []

  return (
    <Card>
      <CardContent className="space-y-4">
        {/* toolbar */}
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-muted-foreground flex items-center gap-1.5 text-xs select-none">
            <input
              type="checkbox"
              className="accent-primary size-3.5"
              checked={folderMode}
              onChange={(e) => setFolderMode(e.target.checked)}
            />
            folder view
          </label>
          <Input
            className="min-w-40 flex-1 font-mono"
            placeholder="prefix filter (e.g. photos/2024/)"
            value={prefix}
            onChange={(e) => setPrefix(e.target.value)}
          />
          <Button variant="outline" onClick={() => list(undefined, true)} disabled={loading}>
            <RefreshCw className={loading ? 'animate-spin' : ''} />
            refresh
          </Button>
        </div>

        {/* breadcrumbs */}
        {folderMode && (
          <div className="flex flex-wrap items-center gap-1 text-sm">
            <button className="text-primary hover:underline" onClick={() => setPrefix('')}>
              {bucket}
            </button>
            {crumbs.map((seg, i) => {
              const p = crumbs.slice(0, i + 1).join('/') + '/'
              return (
                <span key={p} className="flex items-center gap-1">
                  <span className="text-muted-foreground">/</span>
                  <button className="text-primary hover:underline" onClick={() => setPrefix(p)}>
                    {seg}
                  </button>
                </span>
              )
            })}
          </div>
        )}

        {/* uploader */}
        <div className="flex flex-wrap items-center gap-2">
          <Input ref={fileRef} type="file" className="max-w-xs" />
          <Button onClick={upload} disabled={uploadPct !== null}>
            <Upload />
            {uploadPct !== null ? `uploading ${uploadPct}%` : 'Upload (single PUT)'}
          </Button>
          <span className="text-muted-foreground font-mono text-xs">
            → {(folderMode && prefix) || ''}&lt;filename&gt;
          </span>
        </div>
        {uploadPct !== null && <Progress value={uploadPct} />}
        {lastEtag && (
          <p className="text-success text-sm">
            stored ✓ ETag <code className="font-mono">{lastEtag}</code>
          </p>
        )}

        {err && (
          <p className="text-destructive bg-destructive/10 border-destructive/30 rounded-md border px-3 py-2 font-mono text-xs">
            {err}
          </p>
        )}

        {/* table */}
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>key</TableHead>
              <TableHead className="text-right">size</TableHead>
              <TableHead>etag</TableHead>
              <TableHead>last modified</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {prefixes.map((p) => (
              <TableRow key={`p:${p}`} className="cursor-pointer" onClick={() => setPrefix(p)}>
                <TableCell className="font-mono">
                  <span className="flex items-center gap-2">
                    <Folder className="text-muted-foreground size-4" />
                    {p}
                  </span>
                </TableCell>
                <TableCell className="text-muted-foreground text-right">—</TableCell>
                <TableCell className="text-muted-foreground font-mono text-xs">common prefix</TableCell>
                <TableCell className="text-muted-foreground">—</TableCell>
                <TableCell />
              </TableRow>
            ))}
            {objects.map((o) => (
              <TableRow key={`o:${o.key}`}>
                <TableCell className="font-mono">{o.key}</TableCell>
                <TableCell className="text-right tabular-nums">{fmtBytes(o.size)}</TableCell>
                <TableCell className="text-muted-foreground max-w-[200px] truncate font-mono text-xs" title={o.etag}>
                  {o.etag}
                </TableCell>
                <TableCell className="text-muted-foreground">{fmtDate(o.lastModified)}</TableCell>
                <TableCell>
                  <div className="flex items-center justify-end gap-1">
                    <Button variant="ghost" size="icon" asChild>
                      <a
                        href={api.objectUrl(bucket, o.key)}
                        target="_blank"
                        rel="noreferrer"
                        title="download"
                      >
                        <Download />
                      </a>
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="text-muted-foreground hover:text-destructive"
                      title="delete"
                      onClick={() => remove(o.key)}
                    >
                      <Trash2 />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
            {!loading && objects.length === 0 && prefixes.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="text-muted-foreground py-8 text-center">
                  no objects under this prefix
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>

        {token && (
          <Button variant="outline" onClick={() => list(token)} disabled={loading}>
            {loading ? 'loading…' : 'Load more'}
          </Button>
        )}
      </CardContent>
    </Card>
  )
}
