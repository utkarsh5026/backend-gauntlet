import { useEffect, useState } from 'react'
import { Clapperboard, RefreshCw } from 'lucide-react'
import { fetchAssets, masterPlaylistUrl, type Asset } from '@/api'
import { VodPlayer } from '@/components/VodPlayer'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export default function App() {
  const [assets, setAssets] = useState<Asset[]>([])
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [asset, setAsset] = useState('bbb')
  const [src, setSrc] = useState<string | null>(null)

  const loadCatalog = () => {
    fetchAssets()
      .then((a) => {
        setAssets(a)
        setCatalogError(null)
      })
      .catch((e) => setCatalogError(String(e)))
  }

  useEffect(loadCatalog, [])

  const play = (name: string) => {
    setAsset(name)
    setSrc(masterPlaylistUrl(name))
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <header className="flex items-center gap-3">
        <Clapperboard className="text-primary size-6" />
        <div>
          <h1 className="text-xl font-semibold">VOD Streaming</h1>
          <p className="text-muted-foreground text-sm">
            Project 11 · HLS/DASH player — proves adaptive bitrate against your fMP4 segmenter
          </p>
        </div>
      </header>

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 pt-6">
          <div className="grid gap-1.5">
            <Label htmlFor="asset">Asset</Label>
            <Input
              id="asset"
              value={asset}
              onChange={(e) => setAsset(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && play(asset)}
              className="w-56 font-mono"
              placeholder="bbb"
            />
          </div>
          <Button onClick={() => play(asset)}>Play master.m3u8</Button>
          <code className="text-muted-foreground ml-auto text-xs">
            {src ?? 'GET /vod/{asset}/master.m3u8'}
          </code>
        </CardContent>
      </Card>

      {src ? (
        <VodPlayer src={src} />
      ) : (
        <div className="text-muted-foreground rounded-xl border border-dashed py-16 text-center text-sm">
          Pick an asset to start playback.
        </div>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="text-sm">Library</CardTitle>
          <Button size="sm" variant="ghost" onClick={loadCatalog}>
            <RefreshCw className="size-3.5" /> Refresh
          </Button>
        </CardHeader>
        <CardContent>
          {catalogError && (
            <p className="text-muted-foreground text-xs">
              Catalog unavailable ({catalogError}). Is the backend up on :8080?
            </p>
          )}
          {!catalogError && assets.length === 0 && (
            <p className="text-muted-foreground text-xs">No assets loaded.</p>
          )}
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {assets.map((a) => (
              <button
                key={a.asset}
                onClick={() => play(a.asset)}
                className="hover:bg-accent flex flex-col items-start gap-1 rounded-lg border p-3 text-left transition-colors"
              >
                <span className="font-mono text-sm">{a.asset}</span>
                <div className="flex flex-wrap gap-1">
                  {(a.renditions ?? []).map((r) => (
                    <Badge key={r} variant="outline" className="text-[10px]">
                      {r}
                    </Badge>
                  ))}
                </div>
              </button>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
