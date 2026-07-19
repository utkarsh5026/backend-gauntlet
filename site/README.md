# backend-gauntlet site

Lab / portfolio site for the repo — Home, Method, Roadmap, Project 01.

Lives at **https://utkarsh5026.github.io/backend-gauntlet/** after GitHub Pages is enabled.

## Local

```bash
cd site
bun install
bun run dev      # http://localhost:5173/backend-gauntlet/
bun run build   # → dist/
bun run preview
```

Uses Vite `base: '/backend-gauntlet/'` and `HashRouter` so project Pages routing works without SPA rewrites.

## Deploy

[`.github/workflows/pages.yml`](../.github/workflows/pages.yml) builds on push to `master` when `site/**` changes (or via **Actions → GitHub Pages → Run workflow**).

**One-time repo setup:** Settings → Pages → Source → **GitHub Actions**.

Progress SVG: `public/status-dashboard.svg` (refresh from `assets/` when the status dashboard workflow updates it).
