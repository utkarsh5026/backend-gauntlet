import { useEffect, useState } from 'react'
import { Reveal } from '@/components/Reveal'

type ProjectAsset = {
  id: string
  file: string
  kind: string
  title: string
  summary: string
  description: string
}

type AssetManifest = {
  project: string
  updatedAt: string
  assets: ProjectAsset[]
}

async function fetchManifest(dir: string): Promise<AssetManifest | null> {
  try {
    const res = await fetch(`${import.meta.env.BASE_URL}assets/${dir}/assets.json`)
    if (!res.ok) return null
    const data = (await res.json()) as AssetManifest
    return Array.isArray(data.assets) && data.assets.length > 0 ? data : null
  } catch {
    return null
  }
}

/**
 * Renders a project's generated architecture diagrams from its assets.json
 * manifest (produced by the /assets command, mirrored under public/assets/).
 * Renders nothing for projects that have no manifest yet.
 */
export function AssetGallery({ projectDir }: { projectDir: string }) {
  const [manifest, setManifest] = useState<AssetManifest | null>(null)

  useEffect(() => {
    let cancelled = false
    setManifest(null)
    fetchManifest(projectDir).then((data) => {
      if (!cancelled) setManifest(data)
    })
    return () => {
      cancelled = true
    }
  }, [projectDir])

  if (!manifest) return null

  const base = `${import.meta.env.BASE_URL}assets/${projectDir}/`

  return (
    <section className="space-y-8">
      <div className="max-w-2xl space-y-2">
        <h2 className="rule-title text-lg font-bold">architecture</h2>
        <p className="text-[0.85rem] text-fg-muted">
          Diagrams of what's actually built — regenerated when the code they
          depict changes.
        </p>
      </div>
      <div className="space-y-12">
        {manifest.assets.map((asset) => (
          <figure key={asset.id} className="m-0 space-y-4">
            <div className="panel mt-2">
              <p className="panel-title">{asset.kind}</p>
              <img
                src={`${base}${asset.file}`}
                alt={asset.summary}
                loading="lazy"
                className="block w-full"
              />
            </div>
            <figcaption className="max-w-2xl space-y-1.5">
              <p className="m-0 text-[0.9rem] font-bold text-fg">{asset.title}</p>
              <p className="m-0 text-[0.85rem] text-fg-muted">{asset.summary}</p>
              <Reveal label="why it's built this way" className="pt-1">
                <p className="m-0 text-[0.85rem] leading-relaxed text-fg-muted">
                  {asset.description}
                </p>
              </Reveal>
            </figcaption>
          </figure>
        ))}
      </div>
    </section>
  )
}
