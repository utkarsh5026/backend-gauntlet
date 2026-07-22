import { useEffect, useState } from "react";
import { Reveal } from "@/components/Reveal";
import { FileChip } from "@/components/FileChip";
import { REPO_URL } from "@/data/roadmap";

type ProjectAsset = {
  id: string;
  file: string;
  kind: string;
  title: string;
  summary: string;
  /** Plain-language points (problem → fix). Legacy manifests may still use a
   *  single prose string, which we split into sentence bullets. */
  description: string[] | string;
  /** Source files the diagram is drawn from, relative to the project dir. */
  depicts?: string[];
};

type AssetManifest = {
  project: string;
  updatedAt: string;
  assets: ProjectAsset[];
};

async function fetchManifest(dir: string): Promise<AssetManifest | null> {
  try {
    const res = await fetch(`${import.meta.env.BASE_URL}assets/${dir}/assets.json`);
    if (!res.ok) return null;
    const data = (await res.json()) as AssetManifest;
    return Array.isArray(data.assets) && data.assets.length > 0 ? data : null;
  } catch {
    return null;
  }
}

/**
 * Normalize a description into scannable bullet points. Preferred form is an
 * authored array of plain-language points; a legacy prose string is split on
 * sentence boundaries as a fallback.
 */
function toPoints(description: string[] | string): string[] {
  const points = Array.isArray(description)
    ? description
    : description.split(/(?<=[.!?])(?<!\.\.\.)\s+(?=[A-Z"“(0-9])/);
  return points.map((point) => point.trim()).filter(Boolean);
}

/**
 * Renders a project's generated architecture diagrams from its assets.json
 * manifest (produced by the /assets command, mirrored under public/assets/).
 * Renders nothing for projects that have no manifest yet.
 */
export function AssetGallery({ projectDir }: { projectDir: string }) {
  const [manifest, setManifest] = useState<AssetManifest | null>(null);

  useEffect(() => {
    let cancelled = false;
    setManifest(null);
    fetchManifest(projectDir).then((data) => {
      if (!cancelled) setManifest(data);
    });
    return () => {
      cancelled = true;
    };
  }, [projectDir]);

  if (!manifest) return null;

  const base = `${import.meta.env.BASE_URL}assets/${projectDir}/`;
  const fileHref = (path: string) =>
    `${REPO_URL}/blob/master/projects/${projectDir}/${path}`;

  return (
    <section className="space-y-8">
      <div className="max-w-2xl space-y-2">
        <h2 className="rule-title text-lg font-bold">
          architecture
          <span className="text-[0.75rem] font-normal text-fg-muted">
            {manifest.assets.length} diagrams · a walkthrough
          </span>
        </h2>
        <p className="text-[0.85rem] text-fg-muted">
          Each maps to a real part of the build — expand any one for why it works that way.
          Regenerated when the code they depict changes.
        </p>
      </div>
      <div>
        {manifest.assets.map((asset, idx) => (
          <figure
            key={asset.id}
            className="m-0 border-t border-line pt-12 first:border-t-0 first:pt-0"
          >
            <div className="mb-4 flex items-center gap-3">
              <span className="font-display text-[0.9rem] font-bold text-accent">
                {String(idx + 1).padStart(2, "0")}
              </span>
              <span className="h-px flex-1 bg-line sm:max-w-[2rem]" aria-hidden />
              <span className="text-[0.7rem] uppercase tracking-[0.18em] text-fg-muted">
                {asset.kind.replace(/-/g, " ")}
              </span>
              <span className="h-px flex-1 bg-line" aria-hidden />
            </div>

            <figcaption className="mb-5 max-w-2xl space-y-2.5">
              <h3 className="font-display m-0 text-lg font-bold leading-snug tracking-tight text-fg">
                {asset.title}
              </h3>
              <p className="m-0 text-[0.9rem] leading-relaxed text-fg-muted">{asset.summary}</p>
            </figcaption>

            <div className="panel">
              <img
                src={`${base}${asset.file}`}
                alt={asset.summary}
                loading="lazy"
                className="block w-full rounded-[5px]"
              />
            </div>

            <div className="mt-5 max-w-2xl">
              <Reveal label="why this matters">
                <ul className="m-0 list-none space-y-3 border-l border-accent-dim/60 p-0 pl-5 text-[0.875rem] leading-relaxed text-fg-muted">
                  {toPoints(asset.description).map((point, i) => (
                    <li key={i} className="flex gap-3">
                      <span
                        className="mt-[0.55em] h-[5px] w-[5px] shrink-0 rounded-full bg-accent-dim"
                        aria-hidden
                      />
                      <span>{point}</span>
                    </li>
                  ))}
                </ul>
              </Reveal>
            </div>

            {asset.depicts && asset.depicts.length > 0 && (
              <div className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-2">
                <span className="mr-1 text-[0.7rem] uppercase tracking-[0.15em] text-fg-muted">
                  source
                </span>
                {asset.depicts.map((path) => (
                  <FileChip key={path} path={path} href={fileHref(path)} />
                ))}
              </div>
            )}
          </figure>
        ))}
      </div>
    </section>
  );
}
